"""Kill real pinned engines at verified syscall boundaries, then recover.

Instrumentation pauses at a named point but never edits DB data or injects I/O
errors. The actual fault is Docker SIGKILL, matching the contest's process-crash
model. Derived test images must never be used for a competition submission.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tools"))
from test_cpp import Target, checked, same


def arm(target, session, phase):
    code = "from pathlib import Path; p=Path('/data'); (p/'killpoint.reached').unlink(missing_ok=True); (p/'killpoint.control').write_text("+repr(phase)+")"
    if session is not None:
        checked(["docker","exec","--env","LD_PRELOAD=",session.name,"python","-c",code])
    else:
        checked(["docker","run","--rm","--network","none","--env","LD_PRELOAD=","--entrypoint","python",
                 "--mount","type=volume,source="+target.volume+",target=/data",target.args.docker_image,"-c",code])


def await_point(session, phase):
    # A single bounded helper poll; the database process remains paused.
    code="""from pathlib import Path
import time
p=Path('/data/killpoint.reached')
end=time.monotonic()+20
while time.monotonic()<end:
    if p.exists():
        print(p.read_text(),end='')
        raise SystemExit(0)
    time.sleep(.02)
raise SystemExit(3)
"""
    evidence=checked(["docker","exec","--env","LD_PRELOAD=",session.name,"python","-c",code],timeout=25)
    assert evidence.startswith(phase+"\n"), evidence
    assert session.lines.empty(), "SQL response or READY was emitted before the stop point"
    return evidence


def send_unanswered(session, sql):
    request={"id":"in-flight","op":"execute","sql":sql,"search_mode":"exact"}
    session.process.stdin.write((json.dumps(request)+"\n").encode())
    session.process.stdin.flush()


def run_case(image, phase, second_recovery_crash):
    target=Target(SimpleNamespace(docker_image=image,command=None,profile=False,screen_block=None,timeout=40))
    entry={"phase":phase,"second_recovery_crash":second_recovery_crash,"assertions":[]}
    session=None
    def sql(text, **expected):
        response=session.sql(text, **expected)
        entry["assertions"].append({"sql":text,"response":response})
        return response
    try:
        session=target.start()
        for table in ["a","b"]:
            sql(f"CREATE TABLE {table}(id BIGINT PRIMARY KEY,n BIGINT,v VECTOR(2))")
            sql(f"INSERT INTO {table} VALUES(1,0,[0,0]),(2,0,[1,0])",affected=2)
            sql(f"CREATE VECTOR INDEX {table}i ON {table}(v)")
        early=phase in ["before-rows-sync","before-index-sync"]
        if not early:
            sql("BEGIN")
            for table in ["a","b"]:
                sql(f"UPDATE {table} SET n=7,v=[7,0] WHERE id=1",affected=1)
                sql(f"INSERT INTO {table} VALUES(3,7,[8,0])",affected=1)
            statement="COMMIT"
        else:
            statement="UPDATE a SET n=7,v=[7,0] WHERE id=1"
        arm(target,session,phase)
        send_unanswered(session,statement)
        entry["stop_evidence"]=await_point(session,phase)
        entry["response_received_before_kill"]=False
        session.kill()
        if second_recovery_crash:
            arm(target,None,"before-collect-unlink")
            startup_errors=[]
            def start_for_kill():
                try:
                    target.start()
                except Exception as exc:
                    startup_errors.append(str(exc))
            before=len(target.sessions)
            thread=threading.Thread(target=start_for_kill,daemon=True)
            thread.start()
            deadline=time.monotonic()+10
            while len(target.sessions)==before and time.monotonic()<deadline:
                time.sleep(.01)
            assert len(target.sessions)==before+1, "recovery container did not start"
            recovering=target.sessions[-1]
            # Docker run registration may trail the Popen call very briefly.
            time.sleep(.5)
            entry["recovery_stop_evidence"]=await_point(recovering,"before-collect-unlink")
            recovering.kill()
            thread.join(10)
            assert not thread.is_alive(), "stopped recovery reader did not finish"
            assert startup_errors and "unexpected EOF" in startup_errors[0], startup_errors
            entry["recovery_reader_expected_error"]=startup_errors[0]
        session=target.start()
        committed=phase in ["after-catalog-rename","before-collect-unlink"]
        expected=[[1,7,[7.0,0.0]],[2,0,[1.0,0.0]],[3,7,[8.0,0.0]]] if committed else [[1,0,[0.0,0.0]],[2,0,[1.0,0.0]]]
        entry["expected_state"]="all committed" if committed else "all old"
        for table in ["a","b"]:
            sql(f"SELECT id,n,v FROM {table} ORDER BY id",columns=["id","n","v"],rows=expected)
            wanted=[[2],[1],[3]] if committed else [[1],[2]]
            sql(f"SELECT id FROM {table} ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10",columns=["id"],rows=wanted)
            sql(f"SELECT id FROM {table} WHERE n=7 ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10",columns=["id"],rows=[[1],[3]] if committed else [],mode="ann")
            sql(f"INSERT INTO {table} VALUES(1,9,[9,0])",fail=True)
        sql("BEGIN")
        sql("UPDATE a SET n=9 WHERE id=1",affected=1)
        sql("ROLLBACK")
        sql("SELECT id,n,v FROM a ORDER BY id",columns=["id","n","v"],rows=expected)
        session.stop()
        entry["passed"]=True
    except Exception as exc:
        entry["passed"]=False
        entry["error"]=repr(exc)
    finally:
        try: target.cleanup()
        except Exception as exc:
            entry["cleanup_error"]=repr(exc)
            entry["passed"]=False
    return entry


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--images",nargs="+",help="explicit label=image targets")
    parser.add_argument("--python-image",default="minivector-boundary:python-killpoint")
    parser.add_argument("--cpp-image",default="minivector-boundary:cpp-killpoint")
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    images=(dict(item.split("=",1) for item in args.images) if args.images else
            {"python":args.python_image,"cpp":args.cpp_image})
    if args.images and len(images)!=len(args.images):
        parser.error("image labels must be unique")
    report={"timestamp_utc":datetime.now(timezone.utc).isoformat(),"scope":"Verified process SIGKILL boundaries with test-only syscall pause instrumentation","image_tags":images,
            "images":{k:json.loads(subprocess.check_output(["docker","inspect",v]))[0]["Id"] for k,v in images.items()},
            "source_hashes":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),Path(__file__).with_name("killpoint.c"),Path(__file__).with_name("Dockerfile.killpoint")]},
            "cases":[]}
    for label,image in images.items():
        for phase in ["before-rows-sync","before-index-sync","before-catalog-sync","before-catalog-rename","after-catalog-rename","before-collect-unlink"]:
            print("START "+label+" "+phase,flush=True)
            start=time.perf_counter()
            result=run_case(image,phase,second_recovery_crash=(phase=="after-catalog-rename"))
            result.update(label=label,seconds=round(time.perf_counter()-start,3))
            report["cases"].append(result)
            (args.output/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
            print(json.dumps({k:v for k,v in result.items() if k in ["label","phase","passed","error","seconds"]}),flush=True)
    report["passed"]=sum(c["passed"] for c in report["cases"])
    report["asserted_requests"]=sum(len(c["assertions"]) for c in report["cases"])
    (args.output/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"passed":report["passed"],"cases":len(report["cases"]),"asserted_requests":report["asserted_requests"]}),flush=True)
    return int(report["passed"]!=len(report["cases"]))


if __name__=="__main__":
    raise SystemExit(main())
