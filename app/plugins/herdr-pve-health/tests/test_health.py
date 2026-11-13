import importlib.util
import os
from pathlib import Path
import unittest
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'bin'))


def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'bin'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return module


class HealthContracts(unittest.TestCase):
    def test_heavy_groups_are_not_polled_on_every_light_refresh(self):
        d=load('dashboard');groups={k:{'received_at':1000} for k in d.INTERVALS}
        self.assertEqual(d.due(groups,1029),[])
        self.assertEqual(d.due(groups,1030),['fast'])
        self.assertNotIn('smart',d.due(groups,1899))
        self.assertIn('smart',d.due(groups,1900))
        self.assertNotIn('backups',d.due(groups,1299))

    def test_failed_smart_is_unknown_and_retried_without_waiting15minutes(self):
        d=load('dashboard');g={'smart':{'received_at':1000,'error':'unavailable'}}
        self.assertNotIn('smart',d.due(g,1059));self.assertIn('smart',d.due(g,1060))
        self.assertIsNone(d.smart_values({'data':{}})['passed'])

    def test_factory_bad_block_attributes_are_not_relabelled_as_sector_failures(self):
        d=load('dashboard');values=d.smart_values({'data':{'smart_status':{'passed':True},'ata_smart_attributes':{'table':[{'id':169,'name':'Bad_Block_Rate','raw':{'value':9}},{'id':170,'name':'Bad_Blk_Ct_Lat/Erl','raw':{'value':23}}]}}})
        self.assertTrue(values['passed']);self.assertIsNone(values['uncorrectable'])
        self.assertIsNone(values['realloc_events']);self.assertEqual(values['attribute_failures'],[])

    def test_boot_change_and_counter_reset_never_become_rate_spikes(self):
        d=load('dashboard');old={'boot':'a','uptime':30,'ticks':[10]*8,'disks':{},'net':{}}
        new={'boot':'b','uptime':60,'ticks':[30]*8,'disks':{},'net':{}}
        self.assertIsNone(d.metrics(new,old)['cpu'])
        new['boot']='a';new['ticks']=[1]*8
        self.assertIsNone(d.metrics(new,old)['cpu'])

    def test_cpu_and_io_rates_are_real_deltas(self):
        d=load('dashboard')
        old={'boot':'a','uptime':30,'ticks':[10]*8,'disks':{'sda':[10,100,100,10,100,100,100,100]},'net':{}}
        new={'boot':'a','uptime':60,'ticks':[20]*8,'disks':{'sda':[20,200,200,20,200,200,200,200]},'net':{}}
        m=d.metrics(new,old);self.assertEqual(m['cpu'],75);self.assertEqual(m['wait'],12.5)
        self.assertAlmostEqual(m['disk']['sda']['read'],100*512/30)
        self.assertEqual(m['disk']['sda']['await_ms'],10)

    def test_stale_smart_cannot_show_green_pass(self):
        d=load('dashboard');data={'groups':{'smart':{'received_at':1000,'data':[{'device':'sda','model':'test','data':{'smart_status':{'passed':True}}}]}}}
        row=next(x for x in d.lines(data,4000,129) if 'sda test' in x[0])
        self.assertEqual(row[1],d.YELLOW)

    def test_short_pane_announces_hidden_rows(self):
        d=load('dashboard');text=d.render({'groups':{}},False,80,7)
        self.assertIn('increase pane height',text)
        self.assertEqual(len(text.split('\r\n')),7)

    @unittest.skipIf(os.name == 'nt', 'probe.py executes on the remote PVE host (Linux-only modules)')
    def test_native_task_failure_and_active_queue_are_distinguished(self):
        p=load('probe')
        failed="UPID:node:1:2:3:vzdump::root@pam: A command failed: pvesm missing"
        unrelated="UPID:node:1:2:3:aptupdate::root@pam: B OK"
        active="UPID:node:4:5:6:vzdump::root@pam: 0\nUPID:node:1:2:3:vzdump::root@pam: 1 A command failed"
        last,running=p.parse_backup_tasks(failed+'\n'+unrelated,active)
        self.assertEqual(last['status'],'command failed: pvesm missing')
        self.assertEqual(running,1)
        last,_=p.parse_backup_tasks(failed+'\nUPID:node:4:5:6:vzdump::root@pam: C OK',active)
        self.assertEqual(last['status'],'OK')

    @unittest.skipIf(os.name == 'nt', 'probe.py executes on the remote PVE host (Linux-only modules)')
    def test_smart_reads_only_real_disks_and_never_starts_tests(self):
        p=load('probe');calls=[]
        def fake(argv,**kw):
            calls.append(argv)
            if argv[0]=='lsblk':return {'blockdevices':[{'name':'sda','type':'disk','model':'SSD','tran':'sata'},{'name':'nbd0','type':'disk','model':'recovery'},{'name':'loop0','type':'loop','model':'loop'}]},0
            return {'smart_status':{'passed':True}},0
        with patch.object(p,'command',side_effect=fake):result=p.smart()
        self.assertEqual(len(result),1);self.assertEqual(len(calls),2)
        self.assertIn('standby,3',calls[1]);self.assertNotIn('-t',calls[1])
        self.assertNotIn('nbd0',' '.join(calls[1]))


if __name__=='__main__':unittest.main()
