import unittest,csv,io
from app import reconcile
class Reconcile(unittest.TestCase):
 def ok(self,body,posted,balances):self.assertEqual(reconcile('id,account,amount\n'+body),{'posted':posted,'balances':balances})
 def bad(self,text):
  with self.assertRaises(ValueError):reconcile(text)
 def test_empty_header(self):self.ok('',0,{})
 def test_sums_exact(self):self.ok('1,a,0.10\n2,a,0.20\n3,a,-0.30\n',3,{'a':0})
 def test_sorted(self):
  r=reconcile('id,account,amount\nz,z,2\na,a,-1.5\n');self.assertEqual(list(r['balances']),['a','z']);self.assertEqual(r['balances'],{'a':-150,'z':200})
 def test_normalized_replay(self):self.ok(' 1 , café , 01.0 \n1,café,1.00\n',1,{'café':100})
 def test_conflict(self):
  for body in ['1,a,1\n1,a,2','1,a,1\n1,b,1']:self.bad('id,account,amount\n'+body)
 def test_unicode_and_internal_space(self):self.ok('1,東京  café,12.34\n',1,{'東京  café':1234})
 def test_quoted(self):self.ok('"a,b","c,d",1.23\n',1,{'c,d':123})
 def test_escaped_quotes(self):self.ok('1,"a""b",2\n',1,{'a"b':200})
 def test_embedded_newline(self):self.ok('1,"a\nb",3\n',1,{'a\nb':300})
 def test_bom_crlf(self):self.assertEqual(reconcile('\ufeffid,account,amount\r\n1,a,2.5\r\n'),{'posted':1,'balances':{'a':250}})
 def test_large_exact_integer(self):self.ok('1,a,12345678901234567890.99\n',1,{'a':1234567890123456789099})
 def test_zero(self):self.ok('1,a,-0.00\n',1,{'a':0})
 def test_bad_amounts(self):
  for amount in ['','+1','1e3','1_000','NaN','inf','1.001','.5','1.','１２','١','- 1']:
   with self.subTest(amount=amount):self.bad('id,account,amount\n1,a,'+amount)
 def test_bad_header(self):
  for text in ['', 'id,amount,account\n','ID,account,amount\n','id,account,amount,x\n']:self.bad(text)
 def test_bad_rows(self):
  for row in ['1,a','1,a,2,x',',a,1','1, ,1',' ,a,1','1,"unterminated,2']:
   with self.subTest(row=row):self.bad('id,account,amount\n'+row)
 def test_nontext(self):
  for value in [None,2,b'id,account,amount']:self.bad(value)
 def test_empty_physical_rows(self):self.ok('\n1,a,1\n\n',1,{'a':100})
 def test_ten_thousand(self):
  rows=''.join(f'{i},a,0.01\n' for i in range(10000));self.ok(rows,10000,{'a':10000})
