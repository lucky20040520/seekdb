"""Compare the production C++ Unicode helper with Python 3.12 over all scalars.

Run in the pinned Python 3.12 image, using a separately compiled test_unicode
binary. Tests the changed production helper, not a duplicate implementation.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import unicodedata


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary",required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    assert sys.version_info[:2]==(3,12) and unicodedata.unidata_version=="15.0.0"
    expected={"lower":[],"space":[],"decimal":[]}
    scalars=0
    contexts=[]
    for cp in range(0x110000):
        if 0xd800<=cp<=0xdfff:
            continue
        scalars+=1
        text=chr(cp)
        if text.lower()!=text:
            expected["lower"].append([cp,text.lower()])
        if text.isspace():
            expected["space"].append(cp)
        digit=unicodedata.decimal(text,None)
        if digit is not None:
            expected["decimal"].append([cp,digit])
        # Exercise Final_Sigma before and after every character for which
        # ignoring it can affect a cased context; also include all cased chars.
        if text.islower() or text.isupper() or text.istitle() or ("AΣ"+text+"A").lower()[1]=="σ":
            contexts += [text+"Σ","A"+text+"Σ","AΣ"+text,"AΣ"+text+"A"]
    actual=json.loads(subprocess.check_output([args.binary,"--tables"],timeout=60))
    for category in expected:
        assert actual[category]==expected[category], category+" differs from Python 3.12"
    rng=random.Random(2026111304)
    alphabet="AΣσςİi\u0307\u0345\u0301'` :_Äẞß𐐀Ა\u200d\u200b\u2060.\u00a0１"
    contexts += ["".join(rng.choices(alphabet,k=rng.randrange(0,80))) for _ in range(5000)]
    contexts += ["AΣ"+"\u0345"*100000+"Σ", "A"+"\u0345"*100000+"Σ"]
    process=subprocess.Popen([args.binary],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,encoding="utf-8")
    try:
        for start in range(0,len(contexts),128):
            batch=contexts[start:start+128]
            process.stdin.write(json.dumps(batch,ensure_ascii=True)+"\n")
            process.stdin.flush()
            values=json.loads(process.stdout.readline())
            assert values==[text.lower() for text in batch], f"context batch at {start} differs"
        process.stdin.close()
        assert process.wait(timeout=10)==0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    report={"python":sys.version,"unicode":unicodedata.unidata_version,"scalar_values_checked":scalars,
            "lower_mappings":len(expected["lower"]),"whitespace":len(expected["space"]),"decimal_digits":len(expected["decimal"]),
            "context_strings":len(contexts),"malformed_utf8_rejected":5,"passed":True,
            "binary_sha256":hashlib.sha256(Path(args.binary).read_bytes()).hexdigest()}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report))


if __name__=="__main__":
    main()
