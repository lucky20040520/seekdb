-- Run only on a dedicated test instance. This database belongs to this test.
SET NAMES utf8mb4;
SET ob_query_timeout = 60000000;
SET ob_trx_timeout = 60000000;
DROP DATABASE IF EXISTS codex_fts_correctness;
CREATE DATABASE codex_fts_correctness;
USE codex_fts_correctness;
CREATE TABLE docs (
  id BIGINT PRIMARY KEY,
  title VARCHAR(256),
  content TEXT,
  english TEXT
);
INSERT INTO docs VALUES
  (1, '全文索引', 'OceanBase 数据库支持全文索引，分词器支持中文。', 'Database databases running runner audit'),
  (2, '检索测试', '倒排索引 tokenizer，中文和 English 混合。', 'Tokenizer tokenization performance audit audit'),
  (3, '重复词', '数据库 数据库 数据库，全文索引。', 'DATABASE database Database performance'),
  (4, '边界字符', 'ＡＢＣ１２３，一百二十三个。あいうえお 한글 😀', 'A I the and. C++ e-mail test@example.com'),
  (5, '', '', ''),
  (6, NULL, NULL, NULL),
  (7, '标点', '，。！？ -- ++ @ #', '... --- +++ @@@'),
  (8, '量词', '一百米，二十三个，123公斤，三千万元。', 'Search searching searches searched');
CREATE FULLTEXT INDEX ft_ik ON docs(title, content) WITH PARSER ik;
CREATE FULLTEXT INDEX ft_beng ON docs(english) WITH PARSER beng;

SELECT 'tokenize-smart';
SELECT id, TOKENIZE(CAST(content AS CHAR), 'ik', '[{"additional_args":[{"ik_mode":"smart"}]},{"output":"all"}]') FROM docs ORDER BY id;
SELECT 'tokenize-max-word';
SELECT id, TOKENIZE(CAST(content AS CHAR), 'ik', '[{"additional_args":[{"ik_mode":"max_word"}]},{"output":"all"}]') FROM docs ORDER BY id;
SELECT 'tokenize-beng';
SELECT id, TOKENIZE(CAST(english AS CHAR), 'beng', '[{"output":"all"}]') FROM docs ORDER BY id;
SELECT 'natural-language';
SELECT id, ROUND(MATCH(title, content) AGAINST('数据库 全文索引'), 9) AS score
  FROM docs WHERE MATCH(title, content) AGAINST('数据库 全文索引') ORDER BY id;
SELECT id FROM docs WHERE MATCH(english) AGAINST('database audit') ORDER BY id;
SELECT id FROM docs WHERE MATCH(title, content) AGAINST('OceanBase tokenizer') ORDER BY id;
SELECT id FROM docs WHERE MATCH(title, content) AGAINST('不存在的词xyz') ORDER BY id;
SELECT id FROM docs WHERE MATCH(title, content) AGAINST('') ORDER BY id;
SELECT 'boolean-and-score';
SELECT id FROM docs WHERE MATCH(english) AGAINST('+database -audit' IN BOOLEAN MODE) ORDER BY id;
SELECT id FROM docs WHERE MATCH(english) AGAINST('database audit') > 0.01 ORDER BY id;
SELECT id FROM docs WHERE MATCH(english) AGAINST('database audit')
  ORDER BY MATCH(english) AGAINST('database audit') DESC, id LIMIT 2;

SELECT 'dml';
UPDATE docs SET title='已更新', content='新的倒排索引 数据库', english='updated tokenizer' WHERE id=1;
DELETE FROM docs WHERE id=2;
INSERT INTO docs VALUES(9, '新文档', '新的全文索引和数据库', 'new database audit');
SELECT id FROM docs WHERE MATCH(title, content) AGAINST('数据库 全文索引') ORDER BY id;
SELECT id FROM docs WHERE MATCH(english) AGAINST('database audit') ORDER BY id;
START TRANSACTION;
UPDATE docs SET english='rollback sentinel' WHERE id=9;
ROLLBACK;
SELECT id FROM docs WHERE MATCH(english) AGAINST('database audit') ORDER BY id;

SELECT 'rebuild';
DROP INDEX ft_ik ON docs;
CREATE FULLTEXT INDEX ft_ik ON docs(title, content) WITH PARSER ik;
DROP INDEX ft_beng ON docs;
CREATE FULLTEXT INDEX ft_beng ON docs(english) WITH PARSER beng;
SELECT id FROM docs WHERE MATCH(title, content) AGAINST('数据库 全文索引') ORDER BY id;
SELECT id FROM docs WHERE MATCH(english) AGAINST('database audit') ORDER BY id;
DROP DATABASE codex_fts_correctness;
