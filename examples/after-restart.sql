SELECT id,title,embedding FROM docs ORDER BY id;
SELECT id,L2_DISTANCE(embedding,[0,0]) AS distance FROM docs ORDER BY distance LIMIT 10;
SELECT id FROM docs WHERE id=3;
