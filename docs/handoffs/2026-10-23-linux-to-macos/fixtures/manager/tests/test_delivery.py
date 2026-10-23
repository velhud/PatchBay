import unittest,tempfile,concurrent.futures
from pathlib import Path
from ledger import Ledger
from report import summarize
class Delivery(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.path=str(Path(self.tmp.name)/'db'); self.db=Ledger(self.path)
 def tearDown(self):
  self.db.close(); self.tmp.cleanup()
 def test_persistence(self):
  self.assertTrue(self.db.post('a','café',120)); self.assertTrue(self.db.post('b','café',-20)); self.db.close(); self.db=Ledger(self.path); self.assertEqual(self.db.balance('café'),100); self.assertEqual(self.db.balance('missing'),0)
 def test_replay_conflict(self):
  self.assertTrue(self.db.post('a','x',2)); self.assertFalse(self.db.post('a','x',2))
  for account,amount in [('y',2),('x',3)]:
   with self.assertRaises(ValueError): self.db.post('a',account,amount)
  self.assertEqual(self.db.balance('x'),2); self.assertEqual(len(self.db.rows()),1)
 def test_invalid(self):
  for key,account,amount in [('', 'x',1),('  ','x',1),('a',' ',1),('a','x',True),('a','x',1.5),('a','x',0),(None,'x',2),('a',None,2)]:
   with self.assertRaises(ValueError): self.db.post(key,account,amount)
  self.assertEqual(self.db.rows(),[])
 def test_sorted_rows(self):
  self.db.post('b','x',2); self.db.post('a','y',-1); self.assertEqual([r['key'] for r in self.db.rows()],['a','b'])
 def test_two_connections(self):
  other=Ledger(self.path)
  try:
   self.db.post('a','x',3); self.assertFalse(other.post('a','x',3)); other.post('b','x',4); self.assertEqual(self.db.balance('x'),7)
  finally: other.close()
 def test_concurrent_replay(self):
  def post(_):
   d=Ledger(self.path)
   try:return d.post('parallel','x',9)
   finally:d.close()
  with concurrent.futures.ThreadPoolExecutor(max_workers=4) as e: results=list(e.map(post,range(12)))
  self.assertEqual(sum(results),1); self.assertEqual(self.db.balance('x'),9)
 def test_joined(self):
  for row in [('1','z',3),('2','a',7),('3','z',-3)]:self.db.post(*row)
  self.assertEqual(summarize(self.db.rows()),{'a':7,'z':0}); self.assertEqual(list(summarize(self.db.rows())),['a','z'])
class Reports(unittest.TestCase):
 def test_empty(self):self.assertEqual(summarize([]),{})
 def test_replay(self):
  rows=[{'key':'a','account':'é','amount':2}]*2; old=[dict(r) for r in rows]; self.assertEqual(summarize(rows),{'é':2}); self.assertEqual(rows,old)
 def test_conflict(self):
  with self.assertRaises(ValueError):summarize([{'key':'a','account':'x','amount':2},{'key':'a','account':'x','amount':3}])
 def test_invalid(self):
  for r in [{},{'key':'a','account':'x','amount':True},{'key':'a','account':' ','amount':1},{'key':'a','account':'x','amount':0}]:
   with self.assertRaises(ValueError):summarize([r])
