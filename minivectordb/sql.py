"""A small SQL front end, independent of the competition transport protocol."""
import json
import heapq
import itertools
import math
import operator
import re
from dataclasses import dataclass

from .storage import DatabaseError, _vector
from .external_sort import sort_rows


@dataclass
class Result:
    columns: list
    rows: list
    affected: int = 0


_TOKEN = re.compile(r"\s*(?:(--[^\n]*(?:\n|$))|('(?:''|[^'])*')|(`(?:``|[^`])*`)|(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)|([A-Za-z_][A-Za-z_0-9]*)|(<=|>=|<>|!=|[(),;*=<>\[\]]))")


_MAX_INTEGER_DIGITS = 128  # Larger than any finite float32 integer or BIGINT.


class Parser:
    def __init__(self, sql):
        self.tokens = []
        position = 0
        while position < len(sql):
            match = _TOKEN.match(sql, position)
            if not match:
                if sql[position:].strip() == "":
                    break
                raise DatabaseError("invalid SQL token")
            position = match.end()
            comment, string, quoted, number, word, symbol = match.groups()
            if comment:
                continue
            if string is not None:
                self.tokens.append(("literal", string[1:-1].replace("''", "'")))
            elif quoted is not None:
                self.tokens.append(("identifier", quoted[1:-1].replace("``", "`").lower()))
            elif number is not None:
                if any(c in number for c in ".eE"):
                    numeric_value = float(number)
                else:
                    if len(number.lstrip("-")) > _MAX_INTEGER_DIGITS:
                        raise DatabaseError("integer literal too long")
                    numeric_value = int(number)
                self.tokens.append(("literal", numeric_value))
            else:
                self.tokens.append(("word" if word else "symbol", (word or symbol).lower()))
        self.index = 0

    def peek(self, value):
        return self.index < len(self.tokens) and self.tokens[self.index] == ("word" if value[0].isalpha() else "symbol", value.lower())

    def take(self, value):
        if self.peek(value):
            self.index += 1
            return True
        return False

    def expect(self, value):
        if not self.take(value):
            raise DatabaseError("expected " + value)

    def ident(self):
        if self.index >= len(self.tokens) or self.tokens[self.index][0] not in ("word", "identifier"):
            raise DatabaseError("expected identifier")
        name = self.tokens[self.index][1]
        self.index += 1
        return name

    def value(self):
        if self.take("["):
            values = []
            if not self.take("]"):
                while True:
                    values.append(self.value())
                    if self.take("]"):
                        break
                    self.expect(",")
            return values
        if self.index >= len(self.tokens) or self.tokens[self.index][0] != "literal":
            raise DatabaseError("expected literal")
        value = self.tokens[self.index][1]
        self.index += 1
        return value

    def expression(self):
        if self.take("l2_distance"):
            self.expect("(")
            column = self.ident()
            self.expect(",")
            value = self.value()
            self.expect(")")
            return ("distance", column, value)
        return ("column", self.ident())

    def where(self):
        conditions = []
        if self.take("where"):
            while True:
                column = self.ident()
                if self.index >= len(self.tokens) or self.tokens[self.index][1] not in ("=", "<", "<=", ">", ">="):
                    raise DatabaseError("expected comparison")
                comparison = self.tokens[self.index][1]
                self.index += 1
                conditions.append((column, comparison, self.value()))
                if not self.take("and"):
                    break
        return conditions

    def finish(self):
        self.take(";")
        if self.index != len(self.tokens):
            raise DatabaseError("unexpected SQL syntax")

    def parse(self):
        if self.take("create"):
            if self.take("vector"):
                self.expect("index")
                name = self.ident()
                self.expect("on")
                table = self.ident()
                self.expect("(")
                column = self.ident()
                self.expect(")")
                self.finish()
                return ("create_vector_index", name, table, column)
            self.expect("table")
            table = self.ident()
            self.expect("(")
            columns, primary = {}, None
            while True:
                if self.take("primary"):
                    self.expect("key")
                    self.expect("(")
                    key = self.ident()
                    self.expect(")")
                    if primary is not None:
                        raise DatabaseError("multiple primary keys")
                    primary = key
                else:
                    name, kind = self.ident(), self.ident().upper()
                    if name in columns:
                        raise DatabaseError("duplicate column")
                    size = None
                    if kind in ("VARCHAR", "VECTOR"):
                        self.expect("(")
                        size = self.value()
                        self.expect(")")
                    columns[name] = [kind, size]
                    if self.take("primary"):
                        self.expect("key")
                        if primary is not None:
                            raise DatabaseError("multiple primary keys")
                        primary = name
                if self.take(")"):
                    break
                self.expect(",")
            command = ("create", table, columns, primary)
        elif self.take("insert"):
            self.expect("into")
            table = self.ident()
            columns = None
            if self.take("("):
                columns = [self.ident()]
                while self.take(","):
                    columns.append(self.ident())
                self.expect(")")
            self.expect("values")
            rows = []
            while True:
                self.expect("(")
                row = [self.value()]
                while self.take(","):
                    row.append(self.value())
                self.expect(")")
                rows.append(row)
                if not self.take(","):
                    break
            command = ("insert", table, columns, rows)
        elif self.take("select"):
            projections = []
            if self.take("*"):
                projections.append((("star",), "*"))
            else:
                while True:
                    expr = self.expression()
                    alias = self.ident() if self.take("as") else None
                    projections.append((expr, alias or (expr[1] if expr[0] == "column" else "l2_distance")))
                    if not self.take(","):
                        break
            self.expect("from")
            table = self.ident()
            conditions = self.where()
            order = []
            if self.take("order"):
                self.expect("by")
                while True:
                    expr = self.expression()
                    desc = self.take("desc")
                    if not desc:
                        self.take("asc")
                    order.append((expr, desc))
                    if not self.take(","):
                        break
            limit = self.value() if self.take("limit") else None
            command = ("select", table, projections, conditions, order, limit)
        elif self.take("update"):
            table = self.ident()
            self.expect("set")
            changes = {}
            while True:
                column = self.ident()
                if column in changes:
                    raise DatabaseError("duplicate assignment")
                self.expect("=")
                changes[column] = self.value()
                if not self.take(","):
                    break
            command = ("update", table, changes, self.where())
        elif self.take("delete"):
            self.expect("from")
            table = self.ident()
            command = ("delete", table, self.where())
        elif self.take("begin"):
            self.take("transaction")
            command = ("begin",)
        elif self.take("commit"):
            command = ("commit",)
        elif self.take("rollback"):
            command = ("rollback",)
        else:
            raise DatabaseError("unsupported SQL statement")
        self.finish()
        return command


_COMPARISONS = {"=": operator.eq, "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}


def _convert(kind, value):
    if kind == "VECTOR" and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            raise DatabaseError("invalid vector literal") from None
    return value


def _predicate(schema, conditions):
    checks = []
    for column, op, value in conditions:
        if column not in schema["columns"]:
            raise DatabaseError("unknown column")
        kind, _ = schema["columns"][column]
        if kind == "VECTOR" or (kind == "BIGINT" and (type(value) is not int or not -(1 << 63) <= value < (1 << 63))) or (kind == "VARCHAR" and not isinstance(value, str)):
            raise DatabaseError("invalid comparison")
        checks.append((column, _COMPARISONS[op], value))
    return lambda row: all(op(row[column], value) for column, op, value in checks)


def _expression(schema, expr, aliases=None):
    if expr[0] == "column":
        name = expr[1]
        if aliases and name in aliases:
            return aliases[name]
        if name not in schema["columns"]:
            raise DatabaseError("unknown column")
        return lambda row: row[name]
    name, literal = expr[1:]
    if name not in schema["columns"] or schema["columns"][name][0] != "VECTOR":
        raise DatabaseError("distance requires vector column")
    query = _vector(_convert("VECTOR", literal), schema["columns"][name][1])
    return lambda row: math.sqrt(sum((a - b) ** 2 for a, b in zip(row[name], query)))


def execute(db, sql, *, stream=False, search_mode="exact"):
    command = Parser(sql).parse()
    if search_mode not in ("exact", "ann"):
        raise DatabaseError("invalid search mode")
    op = command[0]
    if op in ("begin", "commit", "rollback"):
        getattr(db, op)()
        return Result([], [])
    if op == "create":
        db.create_table(*command[1:])
        return Result([], [])
    if op == "create_vector_index":
        db.create_vector_index(*command[1:])
        return Result([], [])
    table = command[1]
    schema = db.schema(table)
    if op == "insert":
        columns = command[2] if command[2] is not None else list(schema["columns"])
        if len(columns) != len(set(columns)) or set(columns) != set(schema["columns"]):
            raise DatabaseError("columns do not match table")
        rows = []
        for values in command[3]:
            if len(values) != len(columns):
                raise DatabaseError("incorrect value count")
            rows.append({col: _convert(schema["columns"][col][0], value) for col, value in zip(columns, values)})
        db.insert(table, rows)
        return Result([], [], len(rows))
    if op == "update":
        changes = {col: _convert(schema["columns"][col][0], value) if col in schema["columns"] else value for col, value in command[2].items()}
        return Result([], [], db.update(table, _predicate(schema, command[3]), changes))
    if op == "delete":
        return Result([], [], db.delete(table, _predicate(schema, command[2])))
    projections, conditions, order, limit = command[2:]
    if limit is not None and (type(limit) is not int or limit < 0):
        raise DatabaseError("invalid limit")
    if projections == [(("star",), "*")]:
        projections = [(("column", col), col) for col in schema["columns"]]
    functions = [(_expression(schema, expr), alias) for expr, alias in projections]
    aliases = {alias: fn for fn, alias in functions}
    ordering = [(_expression(schema, expr, aliases), desc) for expr, desc in order]
    predicate = _predicate(schema, conditions)
    distance_order = None
    if search_mode == "ann" and len(order) == 1 and not order[0][1] and limit is not None:
        distance_order = order[0][0]
        if distance_order[0] == "column":
            distance_order = next((expr for expr, alias in projections if alias == distance_order[1]), None)
        if distance_order is not None and distance_order[0] != "distance":
            distance_order = None
    rows = (row for row in db.rows(table) if predicate(row))
    if distance_order is not None:
        rows = (entry[2] for entry in db.search(table, distance_order[1], _convert("VECTOR", distance_order[2]), limit, predicate, mode="ann", candidate_budget="auto"))
    elif not ordering:
        rows = itertools.islice(rows, limit) if limit is not None else rows
    elif len(ordering) == 1 and not ordering[0][1] and limit is not None:
        fn = ordering[0][0]
        rows = heapq.nsmallest(limit, rows, key=lambda row: (fn(row), row[schema["primary_key"]]))
    else:
        rows = sort_rows(rows, ordering, schema["primary_key"], db.directory, limit)
    projected = ([fn(row) for fn, _ in functions] for row in rows)
    return Result([alias for _, alias in functions], projected if stream else list(projected))
