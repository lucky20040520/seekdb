"""Reduced-memory stress through the official transport; not a 2 GiB full-scale run.

Run inside the Linux checker container. Each target uses its default entrypoint,
a read-only root, 2 CPUs, 128 MiB, and a fresh disposable data volume.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "protocol_check"))
from _transport import Session


def test(image, output):
    config = json.loads((ROOT/"protocol_check/config.json").read_text(encoding="utf-8"))
    config["memory"] = "128m"
    session = Session(config, output.with_suffix(".stderr.txt"), image=image)
    count, width, dimension = 20000, 8192, 8
    report = {"image":image,"configuration":config,"rows":count,
              "payload_bytes":count*width,"memory_limit_bytes":128*1024**2,
              "scope":"Reduced-memory boundary probe, not the official 2 GiB full-scale workload",
              "steps":[],"passed":False}
    active = list(range(count))

    def request(phase, sql, expected, mode="exact"):
        start = time.perf_counter()
        step = {"phase":phase,"sql_prefix":sql[:180],"mode":mode}
        report["steps"].append(step)
        try:
            response = session.request(sql,mode)
            for key,value in expected.items():
                assert response.get(key)==value, (phase,key,str(response)[:500])
            step["passed"] = True
            return response
        finally:
            step["seconds"] = time.perf_counter()-start

    def topk(phase, mode="exact", minimum=None):
        chosen = [i for i in active if minimum is None or i>=minimum]
        ordered = sorted(chosen,key=lambda i:(i%11,i))[:10]
        where = "" if minimum is None else f"WHERE id>={minimum} "
        return request(phase,"SELECT id FROM t "+where+"ORDER BY L2_DISTANCE(v,'[0,0,0,0,0,0,0,0]') LIMIT 10",
                       {"ok":True,"columns":["id"],"rows":[[i] for i in ordered]},mode)

    try:
        session.start()
        request("schema",f"CREATE TABLE t(id BIGINT PRIMARY KEY,s VARCHAR({width}),v VECTOR({dimension}))",{"ok":True})
        text = "'"+"x"*width+"'"
        for start in range(0,count,100):
            values = ",".join(f"({i},{text},'[{i%11},0,0,0,0,0,0,0]')" for i in range(start,start+100))
            request("insert","INSERT INTO t VALUES "+values,{"ok":True,"affected_rows":100})
            if (start+100)%5000==0:
                print(f"{image}: loaded {start+100}/{count}",flush=True)
        request("external_sort","SELECT id FROM t ORDER BY id DESC",{"ok":True,"rows":[[i] for i in reversed(active)]})
        topk("scan_topk")
        topk("scan_filtered_topk","ann",15000)
        request("build_index","CREATE VECTOR INDEX ix ON t(v)",{"ok":True})
        topk("index_topk","ann")
        topk("index_filtered_topk","exact",15000)
        request("begin","BEGIN",{"ok":True})
        request("update","UPDATE t SET s='changed',v='[20,0,0,0,0,0,0,0]' WHERE id<4",{"ok":True,"affected_rows":4})
        expected = [[i] for i in sorted(range(4,count),key=lambda i:(i%11,i))[:10]]
        request("read_own_write","SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0,0,0,0,0,0,0]') LIMIT 10",{"ok":True,"rows":expected},"ann")
        request("rollback","ROLLBACK",{"ok":True})
        topk("rollback_topk")
        request("begin","BEGIN",{"ok":True})
        request("delete","DELETE FROM t WHERE id<4",{"ok":True,"affected_rows":4})
        request("commit","COMMIT",{"ok":True})
        active = list(range(4,count))
        report["memory_peak_before_kill"] = int(session.docker("exec",session.name,"cat","/sys/fs/cgroup/memory.peak").stdout)
        report["state_before_kill"] = json.loads(session.docker("inspect",session.name).stdout)[0]["State"]
        session.docker("kill","--signal","KILL",session.name)
        session.stop()
        session.start()
        request("restart_scan","SELECT id FROM t ORDER BY id",{"ok":True,"rows":[[i] for i in active]})
        topk("restart_index","ann")
        request("restart_duplicate","INSERT INTO t VALUES(4,'duplicate','[0,0,0,0,0,0,0,0]')",{"ok":False})
        request("begin_uncommitted","BEGIN",{"ok":True})
        request("delete_uncommitted","DELETE FROM t WHERE id<20",{"ok":True,"affected_rows":16})
        session.docker("kill","--signal","KILL",session.name)
        session.stop()
        session.start()
        request("uncommitted_recovery","SELECT id FROM t WHERE id<20 ORDER BY id",{"ok":True,"rows":[[i] for i in range(4,20)]})
        topk("final_index","ann")
        session.shutdown()
        report["passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
        try:
            report["failure_state"] = json.loads(session.docker("inspect",session.name).stdout)[0]["State"]
        except Exception:
            pass
    finally:
        session.close()
        report["max_request_seconds"] = max((s["seconds"] for s in report["steps"]),default=0)
        output.write_text(json.dumps(report,indent=2),encoding="utf-8")
        print(json.dumps({k:report.get(k) for k in ["image","passed","error","max_request_seconds","memory_peak_before_kill"]}),flush=True)
    return report["passed"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--images",nargs="+",required=True,help="explicit label=image targets")
    args = parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    passed = []
    for item in args.images:
        label,image=item.split("=",1)
        passed.append(test(image,args.output/(label+".json")))
    return int(not all(passed))


if __name__=="__main__":
    raise SystemExit(main())
