"""Observe indexed wide-row filtering and transactional exact results."""
import json
import math
import random
import tempfile
from unittest.mock import patch
from minivector.engine import Database
from minivector.sql import execute
from minivector import flat_index, segments

QUERY = ("SELECT id,L2_DISTANCE(v," + str([0]*128) +
         ") AS d FROM t WHERE tag=3 ORDER BY d LIMIT 10;")


def main():
    with tempfile.TemporaryDirectory() as root:
        db = Database(root)
        execute(db, "CREATE TABLE t(id BIGINT PRIMARY KEY,tag BIGINT,p VARCHAR(2048),v VECTOR(128));")
        rng = random.Random(19017)
        records = [[i, i%10, "x"*2048, [rng.uniform(-1,1) for _ in range(128)]]
                   for i in range(10000)]
        for i in range(0,len(records),100):
            db.insert("t",records[i:i+100])
        execute(db,"CREATE VECTOR INDEX idx ON t(v);")

        def checked(stage):
            counts = {"filtered_rows_decoded":0,"distance_candidates":0,"result_rows_fetched":0,
                      "cache_available":None}
            iterator = flat_index.candidates
            at = segments.Reader.at
            fetch = flat_index.read_row
            cached = Database.cached_vectors
            def tracked_candidates(*args,**kwargs):
                for candidate in iterator(*args,**kwargs):
                    counts["distance_candidates"]+=1
                    yield candidate
            def tracked_at(reader,offset):
                counts["filtered_rows_decoded"]+=1
                return at(reader,offset)
            def tracked_fetch(*args,**kwargs):
                counts["result_rows_fetched"]+=1
                return fetch(*args,**kwargs)
            def tracked_cache(database,table):
                value=cached(database,table)
                counts["cache_available"]=value is not None
                return value
            truth=sorted(
                (math.sqrt(math.fsum(x*x for x in row[3])),row[0])
                for row in db.rows(db.table("t")) if row[1]==3
            )[:10]
            with patch.object(flat_index,"candidates",tracked_candidates),                  patch.object(segments.Reader,"at",tracked_at),                  patch.object(flat_index,"read_row",tracked_fetch),                  patch.object(Database,"cached_vectors",tracked_cache):
                result=list(execute(db,QUERY)[1])
            assert result==[[key,distance] for distance,key in truth]
            assert counts["cache_available"] is False
            assert counts["distance_candidates"]==1000
            assert counts["result_rows_fetched"]==10
            assert counts["filtered_rows_decoded"]==10010
            print(json.dumps({"stage":stage,"counts":counts,"topk":result,"verified":True}))
            return result

        before=checked("indexed")
        execute(db,"BEGIN;")
        execute(db,"UPDATE t SET v="+str([0]*128)+" WHERE id=3;")
        changed=checked("updated")
        assert changed[0]==[3,0.0]
        execute(db,"ROLLBACK;")
        assert checked("immediate_rollback")==before
        db.close()
        db=Database(root)
        assert checked("reopened")==before
        db.close()


if __name__=="__main__":
    main()
