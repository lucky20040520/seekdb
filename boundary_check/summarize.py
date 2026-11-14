"""Preserve the verified comparison and provenance alongside the test tools."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT/"protocol-results"
OUT = ROOT/"boundary_check/results/20261113"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    OUT.mkdir(parents=True,exist_ok=False)
    files = {
        "boundary-result.json": RAW/"boundary-20261113/result.json",
        "boundary-steps.json": RAW/"boundary-20261113/steps.json",
        "unicode-result.json": RAW/"boundary-unicode-identifiers-20261113/result.json",
        "unicode-steps.json": RAW/"boundary-unicode-identifiers-20261113/steps.json",
        "public-python.json": RAW/"boundary-public-20261113/python/result.json",
        "public-cpp.json": RAW/"boundary-public-20261113/cpp/result.json",
        "regression-cpp.json": RAW/"boundary-cpp-regression-20261113.json",
        "memory-python.json": RAW/"boundary-memory-20261113/python.json",
        "memory-cpp.json": RAW/"boundary-memory-20261113/cpp.json",
        "source-manifest.json": ROOT/".artifacts/boundary-20261113/manifest.json",
    }
    for name,source in files.items():
        shutil.copyfile(source,OUT/name)
    steps = read(files["boundary-steps.json"])+read(files["unicode-steps.json"])
    matrix = []
    for group in dict.fromkeys(s["group"] for s in steps):
        records = [s for s in steps if s["group"]==group]
        asserted = [s for s in records if s["expected"] is not None]
        row = {"group":group,"steps":len(records),"asserted_steps":len(asserted),
               "compatibility_probes":len(records)-len(asserted),
               "differing_steps":sum(not s["equivalent"] for s in records)}
        for label in ("python","cpp"):
            row[label+"_passed_assertions"] = sum(label not in s["failures"] for s in asserted)
        matrix.append(row)
    stress = {}
    for label in ("python","cpp"):
        result = read(files["memory-"+label+".json"])
        stress[label] = {key:result[key] for key in ["passed","rows","payload_bytes","memory_limit_bytes","memory_peak_before_kill","max_request_seconds"]}
        stress[label].update(steps=len(result["steps"]),oom_killed=result["state_before_kill"]["OOMKilled"],
                             timings_are_single_run_diagnostics=True)
    summary = {
        "date":"2026-11-13","timezone":"Asia/Shanghai",
        "generated_utc":datetime.now(timezone.utc).isoformat(),
        "scope":"Pinned-revision local comparison, not an official submission or hidden-case reproduction",
        "revisions":{name:value["revision"] for name,value in read(files["source-manifest.json"]).items()},
        "remote_head_at_fetch":subprocess.check_output(["git","rev-parse","origin/2026-competition-round2"],cwd=ROOT,text=True).strip(),
        "images":read(files["boundary-result.json"])["images"],
        "matrix":matrix,"total_steps":len(steps),"scope_counts":dict(Counter(s["scope"] for s in steps)),
        "public":{label:{"passed":read(files["public-"+label+".json"])["passed"],"cases":20} for label in ("python","cpp")},
        "existing_cpp_regression":{key:read(files["regression-cpp.json"])[key] for key in ["cases","passed_cases","requests"]},
        "over_memory":stress,
        "conclusions":[
            "No C++ failure in 727 explicitly asserted local steps; Python fails the 5000-digit request-ID assertion and remains alive.",
            "21 additional compatibility probes produce 10 differing responses. They expose Unicode identifier folding, non-ASCII whitespace and non-ASCII digits; one table-query difference is downstream of a rejected insert.",
            "The official package does not publicly define all these lexical/identifier details, so the differences are not identified as confirmed formal-test causes.",
            "Both pinned images pass the 20 public cases and the reduced-memory workload. C++ also passes 16 existing supplementary groups.",
            "The official hidden inputs, full-scale 2 GiB workload and interruption-during-recovery windows were not reproduced. The platform failure cause remains unconfirmed.",
        ],
        "sources":[
            "https://raw.githubusercontent.com/oceanbase/seekdb/2026-competition-round2/mysqltest/README.md",
            "https://raw.githubusercontent.com/oceanbase/seekdb/2026-competition-round2/protocol_check/config.json",
            "https://dev.mysql.com/doc/refman/8.4/en/type-conversion.html",
            "https://dev.mysql.com/doc/refman/8.4/en/out-of-range-and-overflow.html",
            "https://dev.mysql.com/doc/refman/8.4/en/string-literals.html",
            "https://dev.mysql.com/doc/refman/8.4/en/storage-requirements.html",
            "https://dev.mysql.com/doc/refman/8.4/en/identifier-case-sensitivity.html",
            "https://dev.mysql.com/doc/refman/8.4/en/limit-optimization.html",
        ],
        "artifact_sha256":{name:hashlib.sha256((OUT/name).read_bytes()).hexdigest() for name in files},
        "tool_sha256":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/"boundary_check").glob("*.py")},
    }
    (OUT/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"steps":summary["total_steps"],"scope_counts":summary["scope_counts"],"matrix":matrix},ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
