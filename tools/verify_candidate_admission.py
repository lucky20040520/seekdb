"""Isolated admission-policy diagnostic; not SQL end-to-end."""
import json,time,statistics
from collections import OrderedDict
import numpy as np
from minivector.vector import VectorCache

def trial(sequence,policy):
 c=VectorCache();c.np=np;c.scalar={'tag':np.arange(2048,dtype=np.int64)%128}
 c.postings=OrderedDict();c.posting_bytes=0;c.size=65536;c.budget=16*1024*1024
 seen=OrderedDict();seen_writes=seen_evictions=seen_peak=0;hits=writes=evictions=peak=0
 started=time.perf_counter()
 for key in sequence:
  sig=(('tag','=',key),)
  hit=sig in c.postings;hits+=hit
  admit=policy=='original' or (policy in ('second','bounded') and key in seen)
  if policy in ('second','bounded'):
   if key in seen:seen.move_to_end(key)
   else:
    seen[key]=None;seen_writes+=1
    if policy=='bounded' and len(seen)>64:
     seen.popitem(last=False);seen_evictions+=1
   seen_peak=max(seen_peak,len(seen))
  before=set(c.postings)
  budget=c.budget
  if not admit:c.budget=c.size-c.posting_bytes
  indexes=c.candidate_indexes([('tag','=',key)])
  c.budget=budget
  expected=[i for i in range(2048) if i%128==key]
  assert indexes.tolist()==expected
  after=set(c.postings)
  writes+=int(not hit and sig in after)
  evictions+=len(before-after)
  peak=max(peak,len(after))
 return {'seconds_with_truth_checks':time.perf_counter()-started,'hits':hits,'lookups':len(sequence),'writes':writes,'evictions':evictions,'peak_entries':peak,'seen_keys':len(seen),'seen_writes':seen_writes,'seen_evictions':seen_evictions,'seen_peak':seen_peak,'scope':'policy wrapper and independent truth included; no RSS/device I/O or SQL timing'}
if __name__=='__main__':
 workloads={'unique':list(range(500)),'pairs':[k for k in range(250) for _ in range(2)],'hot':[k%8 for k in range(500)]}
 for name,seq in workloads.items():
  for policy in ('off','original','second','bounded'):
   rounds=[trial(seq,policy) for _ in range(3)]
   print(json.dumps({'workload':name,'policy':policy,'rounds':rounds}))
