"""Verify report/script provenance and unchanged instrumented DB payloads."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
HERE=Path(__file__).resolve().parent


def checked(args):
    return subprocess.check_output(args,cwd=ROOT,encoding="utf-8").strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    output=HERE/"results"/"20261113-round2"/"verification.json"
    if output.exists():
        raise SystemExit("refusing to overwrite verification")
    sql=json.loads((output.parent/"result.json").read_text(encoding="utf-8"))
    crash_path=HERE/"results"/"20261113-killpoints"/"result.json"
    crash=json.loads(crash_path.read_text(encoding="utf-8"))
    assert sql["script_sha256"]==digest(HERE/"round2.py")
    assert all(value==digest(HERE/name) for name,value in crash["source_hashes"].items())
    assert all(not value for value in sql["failures"].values())
    assert all(c["passed"] for c in crash["cases"])
    bases={"python":"minivector-boundary:python-f1b9f4e","cpp":"minivector-boundary:cpp-8c2bbcac8"}
    codes={
        "cpp":"import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('/usr/local/bin/minivector').read_bytes()).hexdigest())",
        "python":"import hashlib,pathlib;root=pathlib.Path('/app/minivector');h=hashlib.sha256();[(h.update(str(p.relative_to(root)).encode()),h.update(b'\\0'),h.update(p.read_bytes())) for p in sorted(root.rglob('*.py'))];print(h.hexdigest())",
    }
    payloads={}
    for label,base in bases.items():
        derived="minivector-boundary:"+label+"-killpoint"
        info={}
        for kind,image in [("base",base),("instrumented",derived)]:
            config=json.loads(checked(["docker","inspect",image]))[0]
            value=checked(["docker","run","--rm","--network","none","--env","LD_PRELOAD=","--entrypoint","python",image,"-c",codes[label]])
            info[kind]={"image":image,"id":config["Id"],"entrypoint":config["Config"]["Entrypoint"],"database_sha256":value}
        assert info["base"]["database_sha256"]==info["instrumented"]["database_sha256"]
        assert info["base"]["entrypoint"]==info["instrumented"]["entrypoint"]
        assert info["base"]["id"]==sql["image_ids"][label]
        assert info["instrumented"]["id"]==crash["images"][label]
        payloads[label]=info
    files=[HERE/"round2.py",HERE/"crash_points.py",HERE/"killpoint.c",HERE/"Dockerfile.killpoint",
           HERE/"duplicate_alias_case.sql",HERE/"ROUND2_REPORT.txt",output.parent/"result.json",output.parent/"steps.json",crash_path]
    report={"head":checked(["git","rev-parse","HEAD"]),"tracked_diff":checked(["git","diff","--stat"]),
            "payload_verification":payloads,"file_sha256":{str(p.relative_to(HERE)):digest(p) for p in files},
            "new_contract_steps_per_version":sql["by_scope"]["contract"],
            "compatibility_probes_per_version":sql["by_scope"]["mysql_compatibility"],
            "crash_scenarios_total":len(crash["cases"]),"crash_asserted_requests_total":crash["asserted_requests"],
            "verified_sigkills_total":sum(1+bool(c.get("recovery_stop_evidence")) for c in crash["cases"]),
            "test_containers_remaining":checked(["docker","ps","-a","--filter","name=cpp-regression-","--format","{{.Names}}"])}
    assert not report["tracked_diff"]
    assert not report["test_containers_remaining"]
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:report[k] for k in ["head","new_contract_steps_per_version","compatibility_probes_per_version","crash_scenarios_total","verified_sigkills_total"]}))


if __name__=="__main__":
    main()
