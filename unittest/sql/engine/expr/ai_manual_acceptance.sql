-- Run once against a disposable instance built from ai_func.
-- Start ai_mock_service.py --host 127.0.0.1 --port 18080 --dimension 768
-- in the same WSL environment as the database. No real model/key is required.
CREATE DATABASE IF NOT EXISTS ai_manual;
USE ai_manual;

CALL DBMS_AI_SERVICE.CREATE_AI_MODEL(
  'local_mock_complete', '{"type":"completion","model_name":"fixture-complete"}');
CALL DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT(
  'local_mock_complete_ep', '{"ai_model_name":"local_mock_complete","scope":"all","url":"http://127.0.0.1:18080/ok/chat/completions","access_key":"mock-key","provider":"openai"}');
CALL DBMS_AI_SERVICE.CREATE_AI_MODEL(
  'local_mock_embed', '{"type":"dense_embedding","model_name":"fixture-embed"}');
CALL DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT(
  'local_mock_embed_ep', '{"ai_model_name":"local_mock_embed","scope":"all","url":"http://127.0.0.1:18080/ok/embeddings","access_key":"mock-key","provider":"openai"}');
CALL DBMS_AI_SERVICE.CREATE_AI_MODEL(
  'local_mock_rerank', '{"type":"rerank","model_name":"fixture-rerank"}');
CALL DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT(
  'local_mock_rerank_ep', '{"ai_model_name":"local_mock_rerank","scope":"all","url":"http://127.0.0.1:18080/ok/rerank","access_key":"mock-key","provider":"siliconflow"}');
CALL DBMS_AI_SERVICE.CREATE_AI_MODEL(
  'local_mock_ollama_complete', '{"type":"completion","model_name":"fixture-ollama"}');
CALL DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT(
  'local_mock_ollama_complete_ep', '{"ai_model_name":"local_mock_ollama_complete","scope":"all","url":"http://127.0.0.1:18080/ok/api/generate","provider":"oLlAmA"}');
CALL DBMS_AI_SERVICE.CREATE_AI_MODEL(
  'local_mock_ollama_embed', '{"type":"dense_embedding","model_name":"fixture-ollama-embed"}');
CALL DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT(
  'local_mock_ollama_embed_ep', '{"ai_model_name":"local_mock_ollama_embed","scope":"all","url":"http://127.0.0.1:18080/ok/api/embed","provider":"OLLAMA"}');

-- AI_PROMPT returns a JSON template object; AI_COMPLETE expands that object.
SELECT AI_PROMPT('{0}/{0}/{1}', JSON_ARRAY(1, TRUE), JSON_OBJECT('k', 2)) AS prompt_object;
-- Expected: 你好/你好/世界
SELECT AI_COMPLETE('local_mock_complete', AI_PROMPT('{0}/{0}/{1}', '你好', '世界')) AS expanded_prompt;
-- Expected: hello
SELECT AI_COMPLETE('local_mock_complete', 'hello', NULL) AS completion;
-- Expected: 768 and 3
SELECT JSON_LENGTH(AI_EMBED('local_mock_embed', 'hello')) AS default_dimension,
       JSON_LENGTH(AI_EMBED('local_mock_embed', 'hello', 3)) AS requested_dimension;
-- Expected first document: id a, score 10; all object fields remain present.
SELECT AI_RERANK('local_mock_rerank', 'apple',
  '[{"id":"b","text":"banana"},{"id":"a","text":"apple"}]', 'text') AS reranked_documents;
-- Expected: hello and 768; both endpoints omit access_key.
SELECT AI_COMPLETE('local_mock_ollama_complete', 'hello') AS ollama_completion,
       JSON_LENGTH(AI_EMBED('local_mock_ollama_embed', 'hello')) AS ollama_dimension;

CREATE TABLE IF NOT EXISTS mock_batch(id INT PRIMARY KEY, content TEXT);
INSERT INTO mock_batch VALUES (1, 'first'), (2, NULL), (3, 'third')
  ON DUPLICATE KEY UPDATE content = VALUES(content);
-- Expected dimensions by id: 768, NULL, 768.
SELECT id, JSON_LENGTH(AI_EMBED('local_mock_embed', content)) AS dimension
  FROM mock_batch ORDER BY id;
-- Ordinary SQL: 2 and HELLO.
SELECT 1 + 1 AS arithmetic, UPPER('hello') AS ordinary_string;
