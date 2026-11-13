CREATE TABLE t(id BIGINT PRIMARY KEY,n BIGINT,s VARCHAR(8),v VECTOR(2));
INSERT INTO t VALUES(1,30,'b',[0,0]),(2,10,'a',[10,0]),(3,20,'a',[5,0]);
-- Python f1b9f4e and the repaired C++ version both select id=1 below.
-- Before the compatibility fix, C++ 8c2bbcac8 selected id=2.
-- The local compatibility requirement follows Python's actual behavior;
-- this does not define a general MySQL rule for ambiguous aliases.
SELECT id,L2_DISTANCE(v,[0,0]) AS x,L2_DISTANCE(v,[10,0]) AS x FROM t ORDER BY x LIMIT 1;
CREATE VECTOR INDEX vi ON t(v);
SELECT id,L2_DISTANCE(v,[0,0]) AS x,L2_DISTANCE(v,[10,0]) AS x FROM t ORDER BY x LIMIT 1;
