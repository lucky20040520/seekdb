-- Dedicated test instance only. Compare the complete output across binaries.
SET NAMES utf8mb4;
SET ob_query_timeout = 60000000;
SET ob_trx_timeout = 60000000;
DROP DATABASE IF EXISTS codex_fts_score_usage;
CREATE DATABASE codex_fts_score_usage;
USE codex_fts_score_usage;
CREATE TABLE docs (id BIGINT PRIMARY KEY, body TEXT, tag INT);
INSERT INTO docs VALUES
 (1, 'database audit audit 全文索引 数据库', 1),
 (2, 'database database performance 数据库', 1),
 (3, 'audit performance 全文检索', 2),
 (4, 'DATABASE Audit 数据库 中文', 2),
 (5, 'no matches here', 3),
 (6, '', 3), (7, NULL, 3), (8, 'the and a', 3);
CREATE FULLTEXT INDEX ft_beng ON docs(body) WITH PARSER beng;
CREATE TABLE codes (code VARBINARY(32) PRIMARY KEY, body TEXT);
INSERT INTO codes VALUES
 (0x410042, 'database audit'), (0x610062, 'audit performance'), (0x616263, 'database');
CREATE FULLTEXT INDEX ft_codes ON codes(body) WITH PARSER beng;
CREATE TABLE composite_docs (tenant_id INT, code VARCHAR(32), body TEXT,
 PRIMARY KEY(tenant_id, code));
INSERT INTO composite_docs VALUES
 (1, 'first', 'database audit'), (1, 'second', 'audit'), (2, 'first', 'database');
CREATE FULLTEXT INDEX ft_composite ON composite_docs(body) WITH PARSER beng;
CREATE TABLE tags (id INT PRIMARY KEY);
INSERT INTO tags VALUES (1), (2), (3);

SELECT 'membership';
SELECT COUNT(*) FROM docs WHERE MATCH(body) AGAINST('database audit');
SELECT id FROM docs WHERE MATCH(body) AGAINST('database audit') ORDER BY id;
SELECT id, tag, body FROM docs WHERE MATCH(body) AGAINST('database audit') ORDER BY id;
SELECT id, tag FROM docs WHERE MATCH(body) AGAINST('database audit') AND tag=1 ORDER BY id;
SELECT id FROM docs WHERE MATCH(body) AGAINST('database audit') ORDER BY id LIMIT 2 OFFSET 1;
SELECT COUNT(*) FROM (SELECT id FROM docs WHERE MATCH(body) AGAINST('database audit') LIMIT 2 OFFSET 1) s;
SELECT HEX(code), body FROM codes WHERE MATCH(body) AGAINST('database audit') ORDER BY code;
SELECT tenant_id, code, body FROM composite_docs WHERE MATCH(body) AGAINST('database audit')
 ORDER BY tenant_id, code;
SELECT SUM(id) FROM docs WHERE MATCH(body) AGAINST('database audit');
SELECT id FROM docs WHERE MATCH(body) AGAINST('database audit') AND tag=1 ORDER BY id;
SELECT COUNT(*) FROM (SELECT id FROM docs WHERE MATCH(body) AGAINST('database audit') LIMIT 2) s;
SELECT id FROM docs WHERE MATCH(body) AGAINST('nonexistentxyz') ORDER BY id;
SELECT id FROM docs WHERE MATCH(body) AGAINST('') ORDER BY id;
SELECT id FROM docs WHERE MATCH(body) AGAINST('the and') ORDER BY id;
SELECT id FROM docs WHERE MATCH(body) AGAINST(NULL) ORDER BY id;

SELECT 'numeric-score-fallback';
SELECT id, ROUND(MATCH(body) AGAINST('database audit'), 9) FROM docs
 WHERE MATCH(body) AGAINST('database audit') ORDER BY id;
SELECT id, IF(MATCH(body) AGAINST('database audit'), 1, 0) FROM docs
 WHERE MATCH(body) AGAINST('database audit') ORDER BY id;
SELECT id FROM docs WHERE MATCH(body) AGAINST('database audit') > 0 ORDER BY id;
SELECT id FROM docs WHERE MATCH(body) AGAINST('database audit') > 0.1 ORDER BY id;
SELECT id, ROUND(MATCH(body) AGAINST('database audit'), 9) AS score FROM docs
 WHERE MATCH(body) AGAINST('database audit') ORDER BY score DESC, id LIMIT 3;
SELECT tag, COUNT(*) FROM docs WHERE MATCH(body) AGAINST('database audit') GROUP BY tag ORDER BY tag;
SELECT id FROM docs WHERE MATCH(body) AGAINST('database audit') OR tag=3 ORDER BY id;
SELECT d.id FROM docs d JOIN tags t ON d.tag=t.id
 WHERE MATCH(d.body) AGAINST('database audit') ORDER BY d.id;
SELECT id FROM docs WHERE MATCH(body) AGAINST('+database -audit' IN BOOLEAN MODE) ORDER BY id;
SELECT id FROM docs WHERE MATCH(body) AGAINST('database audit' IN BOOLEAN MODE) ORDER BY id;

SELECT 'prepared-parameters';
PREPARE presence_stmt FROM 'SELECT id FROM docs WHERE MATCH(body) AGAINST(?) ORDER BY id';
SET @term='database audit'; EXECUTE presence_stmt USING @term;
SET @term='performance'; EXECUTE presence_stmt USING @term;
SET @term=''; EXECUTE presence_stmt USING @term;
SET @term=NULL; EXECUTE presence_stmt USING @term;
DEALLOCATE PREPARE presence_stmt;
PREPARE score_stmt FROM 'SELECT id FROM docs WHERE MATCH(body) AGAINST(?) > ? ORDER BY id';
SET @term='database audit', @threshold=0; EXECUTE score_stmt USING @term, @threshold;
SET @threshold=0.1; EXECUTE score_stmt USING @term, @threshold;
SET @threshold=100; EXECUTE score_stmt USING @term, @threshold;
DEALLOCATE PREPARE score_stmt;

SELECT 'rescan-and-dml';
UPDATE docs SET body='audit audit', tag=2 WHERE id=2;
DELETE FROM docs WHERE id=3;
INSERT INTO docs VALUES(9, 'new database audit', 1);
SELECT id FROM docs WHERE MATCH(body) AGAINST('database audit') ORDER BY id;
SELECT id, ROUND(MATCH(body) AGAINST('database audit'), 9) FROM docs
 WHERE MATCH(body) AGAINST('database audit') ORDER BY id;
-- Retain this dedicated fixture for execution-plan assertions.

SELECT 'prepared-membership-count';
PREPARE presence_count FROM 'SELECT COUNT(*) FROM docs WHERE MATCH(body) AGAINST(?)';
PREPARE presence_rows FROM 'SELECT id, tag FROM docs WHERE MATCH(body) AGAINST(?) ORDER BY id';
SET @term='database'; EXECUTE presence_count USING @term;
EXECUTE presence_rows USING @term;
SET @term='database audit performance word000 word001 word002 word003 word004 word005 word006 word007 word008 word009 word010 word011 word012 word013 word014 word015 word016 word017 word018 word019 word020 word021 word022 word023 word024 word025 word026 word027 word028 word029 word030 word031 word032 word033 word034 word035 word036 word037 word038 word039 word040 word041 word042 word043 word044 word045 word046 word047 word048 word049 word050 word051 word052 word053 word054 word055 word056 word057 word058 word059 word060 word061 word062 word063 word064 word065 word066 word067 word068 word069 word070 word071 word072 word073 word074 word075 word076 word077 word078 word079 word080 word081 word082 word083 word084 word085 word086 word087 word088 word089 word090 word091 word092 word093 word094 word095 word096 word097 word098 word099 word100 word101 word102 word103 word104 word105 word106 word107 word108 word109 word110 word111 word112 word113 word114 word115 word116 word117 word118 word119 word120 word121 word122 word123 word124 word125 word126 word127 word128 word129 word130 word131 word132 word133 word134 word135 word136 word137 word138 word139 word140 word141 word142 word143 word144 word145 word146 word147 word148 word149 word150 word151 word152 word153 word154 word155 word156 word157 word158 word159 word160 word161 word162 word163 word164 word165 word166 word167 word168 word169 word170 word171 word172 word173 word174 word175 word176 word177 word178 word179 word180 word181 word182 word183 word184 word185 word186 word187 word188 word189 word190 word191 word192 word193 word194 word195 word196 word197 word198 word199 word200 word201 word202 word203 word204 word205 word206 word207 word208 word209 word210 word211 word212 word213 word214 word215 word216 word217 word218 word219 word220 word221 word222 word223 word224 word225 word226 word227 word228 word229 word230 word231 word232 word233 word234 word235 word236 word237 word238 word239 word240 word241 word242 word243 word244 word245 word246 word247 word248 word249 word250 word251 word252 word253 word254 word255 word256 word257 word258 word259 word260 word261 word262 word263 word264 word265 word266 word267 word268 word269 word270 word271 word272 word273 word274 word275 word276 word277 word278 word279 word280 word281 word282 word283 word284 word285 word286 word287 word288 word289 word290 word291 word292 word293 word294 word295 word296 word297 word298 word299'; EXECUTE presence_count USING @term;
EXECUTE presence_rows USING @term;
SET @term='audit'; EXECUTE presence_count USING @term;
EXECUTE presence_rows USING @term;
DEALLOCATE PREPARE presence_count;
DEALLOCATE PREPARE presence_rows;

SELECT 'empty-membership-counts';
SELECT COUNT(*) FROM docs WHERE MATCH(body) AGAINST('');
SELECT COUNT(*) FROM docs WHERE MATCH(body) AGAINST('nonexistentxyz');
SELECT COUNT(*) FROM docs WHERE MATCH(body) AGAINST('the and');
SELECT COUNT(*) FROM docs WHERE MATCH(body) AGAINST(NULL);
DELETE FROM docs;
SELECT COUNT(*) FROM docs WHERE MATCH(body) AGAINST('database audit');
