/*
 * Copyright (c) 2025 OceanBase.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#define USING_LOG_PREFIX SQL_ENG

#include "sql/engine/basic/ob_function_table_op.h"
#include "sql/engine/ob_exec_context.h"
#include "sql/engine/expr/ob_expr_lob_utils.h"
#include "sql/engine/basic/file_csv_scan.h"
#include "sql/engine/basic/file_jsonl_scan.h"
#include "sql/engine/basic/file_parquet_scan.h"
#include "sql/engine/basic/file_glob_scan.h"
#include "sql/session/ob_sql_session_info.h"
#include "common/timezone/ob_time_convert.h"
#include <charconv>
#include <new>


namespace oceanbase
{
using namespace common;
namespace sql
{

struct FileCsvExecState
{
  FileCsvScan scan_;
  FileJsonlScan json_scan_;
  FileParquetScan parquet_scan_;
  FileGlobScan glob_scan_;
  std::vector<FileCsvField> row_;
};

static ObObjType file_csv_obj_type(FileValueType type)
{
  switch (type) {
    case FileValueType::BOOLEAN: return ObTinyIntType;
    case FileValueType::BIGINT: return ObIntType;
    case FileValueType::DOUBLE: return ObDoubleType;
    case FileValueType::DATE: return ObDateType;
    case FileValueType::DATETIME: return ObDateTimeType;
    case FileValueType::NULL_VALUE: return ObNullType;
    case FileValueType::VARCHAR: return ObVarcharType;
  }
  return ObMaxType;
}

OB_SERIALIZE_MEMBER((ObFunctionTableSpec, ObOpSpec), value_expr_, column_exprs_, has_correlated_expr_,
                    is_file_csv_, is_file_jsonl_, is_file_parquet_, is_file_glob_, file_csv_header_, file_column_indexes_, file_column_names_, file_column_types_,
                    file_column_nullable_);

int ObFunctionTableOp::inner_open()
{
  int ret = OB_SUCCESS;
  node_idx_ = 0;
  already_calc_ = false;
  if (OB_ISNULL(MY_SPEC.value_expr_)) {
    ret = OB_ERR_UNEXPECTED;
  } else if (MY_SPEC.is_file_csv_) {
    if (OB_FAIL(inner_open_file_csv())) {
    } else {
      next_row_func_ = &ObFunctionTableOp::inner_get_next_row_file_csv;
    }
  } else if (ObExtendType == MY_SPEC.value_expr_->datum_meta_.type_) {
    next_row_func_ = &ObFunctionTableOp::inner_get_next_row_udf;
  } else {
    next_row_func_ = &ObFunctionTableOp::inner_get_next_row_sys_func;
  }
  return ret;
}

int ObFunctionTableOp::inner_rescan()
{
  int ret = OB_SUCCESS;
  if (OB_FAIL(ObOperator::inner_rescan())) {
  } else if (MY_SPEC.is_file_csv_) {
    if (OB_ISNULL(file_csv_state_) ||
        (MY_SPEC.is_file_glob_ ? file_csv_state_->glob_scan_.rescan() :
         (MY_SPEC.is_file_parquet_ ? file_csv_state_->parquet_scan_.rescan() :
         (MY_SPEC.is_file_jsonl_ ? file_csv_state_->json_scan_.rescan() : file_csv_state_->scan_.rescan())))
            != FileCsvScanStatus::OK) {
      ret = OB_ERR_UNEXPECTED;
      LOG_WARN("file CSV rescan failed or file changed", K(ret));
    }
  } else {
    node_idx_ = 0;
    if (MY_SPEC.has_correlated_expr_) {
      row_count_ = 0;
      col_count_ = 0;
      value_table_ = NULL;
      already_calc_ = false;
    }
  }
  return ret;
}

int ObFunctionTableOp::inner_close()
{
  int ret = OB_SUCCESS;
  node_idx_ = 0;
  row_count_ = 0;
  col_count_ = 0;
  value_table_ = NULL;
  delete file_csv_state_;
  file_csv_state_ = nullptr;
  return ret;
}

void ObFunctionTableOp::destroy()
{
  delete file_csv_state_;
  file_csv_state_ = nullptr;
  ObOperator::destroy();
}

int ObFunctionTableOp::inner_open_file_csv()
{
  int ret = OB_SUCCESS;
  ObSQLSessionInfo *session = ctx_.get_my_session();
  ObDatum *path_datum = nullptr;
  ObString secure_dir;
  bool enabled = false;
  if (OB_ISNULL(session) || OB_ISNULL(MY_SPEC.value_expr_)) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_FAIL(session->get_enable_file_sql(enabled))) {
  } else if (!enabled) {
    ret = OB_NOT_SUPPORTED;
    LOG_USER_ERROR(OB_NOT_SUPPORTED, "file SQL when enable_file_sql is off");
  } else if (OB_FAIL(session->get_secure_file_priv(secure_dir))) {
  } else if (secure_dir.empty()) {
    ret = OB_ERR_NO_PRIVILEGE;
    LOG_USER_ERROR(OB_ERR_NO_PRIVILEGE, "FILE");
  } else if (OB_FAIL(MY_SPEC.value_expr_->eval(eval_ctx_, path_datum))) {
  } else if (OB_ISNULL(path_datum) || path_datum->is_null()) {
    ret = OB_INVALID_ARGUMENT;
  } else if (OB_ISNULL(file_csv_state_ = new (std::nothrow) FileCsvExecState())) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
  } else {
    const ObString path = path_datum->get_string();
    const std::string root(secure_dir.ptr(), secure_dir.length());
    const std::string filename(path.ptr(), path.length());
    const FileCsvScanStatus status = path.empty() ? FileCsvScanStatus::INVALID :
        (MY_SPEC.is_file_glob_ ? file_csv_state_->glob_scan_.open(enabled, root, filename) : MY_SPEC.is_file_jsonl_
          ? file_csv_state_->json_scan_.open(enabled, root, filename, 16 * 1024 * 1024, 4096) : MY_SPEC.is_file_parquet_
          ? file_csv_state_->parquet_scan_.open(enabled, root, filename)
          : file_csv_state_->scan_.open(enabled, root, filename, MY_SPEC.file_csv_header_,
                                        16 * 1024 * 1024, 4096));
    if (status == FileCsvScanStatus::ACCESS_DENIED) {
      ret = OB_ERR_NO_PRIVILEGE;
      LOG_USER_ERROR(OB_ERR_NO_PRIVILEGE, "FILE");
    } else if (status != FileCsvScanStatus::OK) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("failed to open file CSV scan", K(ret), K(static_cast<int>(status)));
    } else {
      const auto &columns = MY_SPEC.is_file_glob_ ? file_csv_state_->glob_scan_.columns() :
          (MY_SPEC.is_file_parquet_ ? file_csv_state_->parquet_scan_.columns() :
          (MY_SPEC.is_file_jsonl_ ? file_csv_state_->json_scan_.columns() : file_csv_state_->scan_.columns()));
      if (columns.size() != static_cast<size_t>(MY_SPEC.file_column_names_.count()) ||
          columns.size() != static_cast<size_t>(MY_SPEC.file_column_types_.count()) ||
          columns.size() != static_cast<size_t>(MY_SPEC.file_column_nullable_.count()) ||
          MY_SPEC.file_column_indexes_.count() != MY_SPEC.column_exprs_.count()) {
        ret = OB_ERR_UNEXPECTED;
      }
      for (size_t i = 0; OB_SUCC(ret) && i < columns.size(); ++i) {
        const ObString name(columns[i].name.size(), columns[i].name.data());
        if (name != MY_SPEC.file_column_names_.at(i) ||
            static_cast<int64_t>(file_csv_obj_type(columns[i].inference.type())) != MY_SPEC.file_column_types_.at(i) ||
            static_cast<int64_t>(columns[i].inference.nullable()) != MY_SPEC.file_column_nullable_.at(i)) {
          ret = OB_ERR_UNEXPECTED;
          LOG_WARN("file CSV schema changed since query planning", K(ret), K(i));
        }
      }
    }
  }
  return ret;
}

int ObFunctionTableOp::inner_get_next_row_file_csv()
{
  int ret = OB_SUCCESS;
  clear_evaluated_flag();
  if (OB_ISNULL(file_csv_state_)) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_FAIL(ctx_.check_status())) {
  } else {
    const FileCsvScanStatus status = MY_SPEC.is_file_glob_ ? file_csv_state_->glob_scan_.next(file_csv_state_->row_) : MY_SPEC.is_file_parquet_
        ? file_csv_state_->parquet_scan_.next(file_csv_state_->row_) : MY_SPEC.is_file_jsonl_
        ? file_csv_state_->json_scan_.next(file_csv_state_->row_)
        : file_csv_state_->scan_.next(file_csv_state_->row_);
    if (status == FileCsvScanStatus::END) {
      ret = OB_ITER_END;
    } else if (status != FileCsvScanStatus::ROW) {
      ret = OB_ERR_UNEXPECTED;
      LOG_WARN("file CSV scan failed or file changed", K(ret), K(static_cast<int>(status)));
    }
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < MY_SPEC.column_exprs_.count(); ++i) {
    ObExpr *expr = MY_SPEC.column_exprs_.at(i);
    const int64_t index = MY_SPEC.file_column_indexes_.at(i);
    if (OB_ISNULL(expr) || index < 0 || index >= static_cast<int64_t>(file_csv_state_->row_.size())) {
      ret = OB_ERR_UNEXPECTED;
    } else {
      const FileCsvField &field = file_csv_state_->row_[index];
      ObDatum &datum = expr->locate_datum_for_write(eval_ctx_);
      const std::string &text = field.value;
      const bool is_null = !field.quoted && text.empty();
      if (is_null || expr->datum_meta_.type_ == ObNullType) {
        datum.set_null();
      } else if (expr->datum_meta_.type_ == ObVarcharType) {
        if (text.empty()) {
          datum.set_string("", 0);
        } else {
          char *buffer = expr->get_str_res_mem(eval_ctx_, text.size());
          if (OB_ISNULL(buffer)) {
            ret = OB_ALLOCATE_MEMORY_FAILED;
          } else {
            MEMCPY(buffer, text.data(), text.size());
            datum.set_string(buffer, text.size());
          }
        }
      } else if (expr->datum_meta_.type_ == ObTinyIntType || expr->datum_meta_.type_ == ObIntType ||
                 expr->datum_meta_.type_ == ObDoubleType) {
        const bool boolean = FileTypeInference::infer_text(text) == FileValueType::BOOLEAN;
        const int64_t boolean_value = boolean && (text[0] == 't' || text[0] == 'T') ? 1 : 0;
        const char *begin = text.data();
        const char *end = begin + text.size();
        if (begin != end && *begin == '+') ++begin;
        if (expr->datum_meta_.type_ == ObDoubleType) {
          double value = static_cast<double>(boolean_value);
          if (!boolean) {
            const auto parsed = std::from_chars(begin, end, value, std::chars_format::general);
            if (parsed.ec != std::errc() || parsed.ptr != end || !std::isfinite(value)) ret = OB_INVALID_ARGUMENT;
          }
          if (OB_SUCC(ret)) datum.set_double(value);
        } else {
          int64_t value = boolean_value;
          if (!boolean) {
            const auto parsed = std::from_chars(begin, end, value);
            if (parsed.ec != std::errc() || parsed.ptr != end) ret = OB_INVALID_ARGUMENT;
          }
          if (OB_SUCC(ret)) datum.set_int(value);
        }
      } else if (expr->datum_meta_.type_ == ObDateType) {
        int32_t value = 0;
        if (OB_FAIL(ObTimeConverter::str_to_date(ObString(text.size(), text.data()), value))) {
        } else {
          datum.set_date(value);
        }
      } else if (expr->datum_meta_.type_ == ObDateTimeType) {
        int64_t value = 0;
        ObTimeConvertCtx convert_ctx(nullptr, false);
        std::string normalized = text;
        if (normalized.size() > 10 && normalized[10] == 'T') normalized[10] = ' ';
        if (OB_FAIL(ObTimeConverter::str_to_datetime(
                ObString(normalized.size(), normalized.data()), convert_ctx, value))) {
        } else {
          datum.set_datetime(value);
        }
      } else {
        ret = OB_NOT_SUPPORTED;
      }
      if (OB_SUCC(ret)) expr->set_evaluated_projected(eval_ctx_);
    }
  }
  return ret;
}


int ObFunctionTableOp::get_current_result(ObObj &result)
{
  int ret = OB_SUCCESS;
  void *data = NULL;
  CK (already_calc_);
  if (node_idx_ < 0 || node_idx_ >= row_count_ * col_count_) {
    ret = OB_ERR_UNEXPECTED;
    LOG_WARN("fail to get current result in table function",
              K(node_idx_), K(row_count_), K(col_count_));
  }
  do {
    CK (node_idx_ >= 0);
    if (OB_SUCC(ret) && node_idx_ >= row_count_) {
      ret = OB_ITER_END;
    }
    CK (OB_NOT_NULL(value_table_));
    OX (data = value_table_->get_data());
    CK (OB_NOT_NULL(data));
    OX (result = (static_cast<ObObj*>(data))[node_idx_++]);
  } while (OB_SUCC(ret) && result.get_meta().get_type() == ObMaxType);
  return ret;
}

int ObFunctionTableOp::inner_get_next_row()
{
  return (this->*next_row_func_)();
}

int ObFunctionTableOp::inner_get_next_row_udf()
{
  int ret = OB_SUCCESS;
  ObPhysicalPlanCtx *plan_ctx = nullptr;
  clear_evaluated_flag();
  if (OB_ISNULL(plan_ctx = ctx_.get_physical_plan_ctx())) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_ISNULL(MY_SPEC.value_expr_)) {
    ret = OB_ERR_UNEXPECTED;
  } else if (ObExtendType != MY_SPEC.value_expr_->datum_meta_.type_) {
    ret = OB_ERR_UNEXPECTED;
  } else if (FALSE_IT(plan_ctx->set_autoinc_id_tmp(0))) {
  } else if (OB_FAIL(ctx_.check_status())) {
  } else {
    ObDatum *value = nullptr;
    if (!already_calc_) {
      if (OB_FAIL(MY_SPEC.value_expr_->eval(eval_ctx_, value))) {
      } else if (value->is_null()) {
        //do nothing
      } else if (OB_ISNULL(value_table_ 
                 = reinterpret_cast<pl::ObPLCollection*>(value->get_ext()))) {
        ret = OB_ERR_UNEXPECTED;
      } else {
        row_count_ = value_table_->is_inited() ? value_table_->get_count() : 0;
        col_count_ = value_table_->get_column_count();
      }
      already_calc_ = true;
    }
    if (OB_SUCC(ret) && OB_UNLIKELY(node_idx_ >= row_count_)) {
      ret = OB_ITER_END;
    }
    CK (MY_SPEC.column_exprs_.count() >= col_count_);
    ObObj obj_stack[col_count_];
    if (OB_SUCC(ret)) {
      if (nullptr != value_table_ 
          && ObExtendType == value_table_->get_element_type().get_obj_type()) {
        pl::ObPLComposite *composite = NULL;
        pl::ObPLRecord *record = NULL;
        ObObj record_obj;
        OZ (get_current_result(record_obj));
        if (OB_FAIL(ret)) {
        } else if (ObUserDefinedSQLType == record_obj.get_type()) {
          obj_stack[0] = record_obj;   
        } else if (record_obj.is_pl_extend()) {
          CK (OB_NOT_NULL(composite = reinterpret_cast<pl::ObPLComposite*>(record_obj.get_ext())));
          if (OB_SUCC(ret)) {
            if (composite->is_record()) {
              OX (record = static_cast<pl::ObPLRecord*>(composite));
              CK (record->get_count() == col_count_);
              for (int64_t i = 0; OB_SUCC(ret) && i < col_count_; ++i) {
                OZ (record->get_element(i, obj_stack[i]));
              }
            } else if (composite->is_collection()) {
              OX (obj_stack[0] = record_obj);
            } else {
              ret = OB_ERR_UNEXPECTED;
            }
          }
        } else {
          ret = OB_ERR_UNEXPECTED;
          LOG_ERROR("unexpected here", K(ret), K(record_obj), K(record_obj.meta_));
        }
      } else {
        for (int64_t i = 0; OB_SUCC(ret) && i < col_count_; ++i) {
          if (OB_FAIL(get_current_result(obj_stack[i]))) {
          }
        }
      }
    }
    if (OB_SUCC(ret)) {
      for (int64_t i = 0; OB_SUCC(ret) && i < col_count_; ++i) {
        if (obj_stack[i].is_null()) {
          MY_SPEC.column_exprs_.at(i)->locate_datum_for_write(eval_ctx_).set_null();
        } else {
          const ObObjDatumMapType &datum_map = MY_SPEC.column_exprs_.at(i)->obj_datum_map_;
          ObExpr * const &expr = MY_SPEC.column_exprs_.at(i);
          ObDatum &datum = expr->locate_datum_for_write(eval_ctx_);
          if (OB_FAIL(datum.from_obj(obj_stack[i], datum_map))) {
          } else if (is_lob_storage(obj_stack[i].get_type()) &&
                     OB_FAIL(ob_adjust_lob_datum(get_exec_ctx(), obj_stack[i],
                                                 expr->obj_meta_, datum_map,
                                                 get_exec_ctx().get_allocator(), datum))) {
          }
        }
        if (OB_SUCC(ret)) {
          MY_SPEC.column_exprs_.at(i)->set_evaluated_projected(eval_ctx_);
        }
      }
    }
  }
  return ret;
}

int ObFunctionTableOp::inner_get_next_row_sys_func()
{
  int ret = OB_SUCCESS;
  ObPhysicalPlanCtx *plan_ctx = nullptr;
  ObDatum *value = nullptr;
  clear_evaluated_flag();
  if (OB_ISNULL(plan_ctx = ctx_.get_physical_plan_ctx())) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_FAIL(ctx_.check_status())) {
  } else if (OB_FAIL(MY_SPEC.value_expr_->eval(eval_ctx_, value))) {
    if (OB_ITER_END != ret) {
    }
  } else {
    MY_SPEC.column_exprs_.at(0)->locate_datum_for_write(eval_ctx_).set_datum(*value);
    MY_SPEC.column_exprs_.at(0)->set_evaluated_projected(eval_ctx_);
  }
  return ret;
}


} // end namespace sql
} // end namespace oceanbase
