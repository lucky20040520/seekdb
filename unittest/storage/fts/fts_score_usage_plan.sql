-- Run after fts_score_usage.sql on a dedicated instance.
USE codex_fts_score_usage;
-- The membership scan must report calc_relevance=false.
EXPLAIN EXTENDED SELECT COUNT(*) FROM docs WHERE MATCH(body) AGAINST('database audit');
-- These retain scoring; numeric thresholds may use has_functional_lookup=true.
EXPLAIN EXTENDED SELECT id, MATCH(body) AGAINST('database audit') FROM docs WHERE MATCH(body) AGAINST('database audit');
EXPLAIN EXTENDED SELECT id FROM docs WHERE MATCH(body) AGAINST('database audit') > 0.1;
EXPLAIN EXTENDED SELECT id FROM docs WHERE MATCH(body) AGAINST('database audit') ORDER BY MATCH(body) AGAINST('database audit') DESC LIMIT 2;
EXPLAIN EXTENDED SELECT id FROM docs WHERE MATCH(body) AGAINST('+database -audit' IN BOOLEAN MODE);
