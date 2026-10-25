import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bin'))
import dashboard as d
import presentation as ui


def healthy():
    now=10000
    groups={key:{'received_at':now,'data':{}} for key in d.INTERVALS}
    groups['fast']['data']={'hostname':'TEST','mem':{'MemTotal':100,'MemAvailable':50},'net':{},'sensors':[]}
    groups['storage']['data']={'filesystems':[{'mount':'/','percent':22,'free':100,'readonly':False}]}
    groups['smart']['data']=[{'device':'sda','data':{'smart_status':{'passed':True}}}]
    groups['vms']['data']=[{'vmid':i,'name':'test-vm','status':'running','cpu':.04,'mem':100} for i in range(100,105)]
    groups['agents']['data']=[{'Id':'office','ActiveState':'active'}]
    groups['apps']['data']={'1C TCP':True,'host DNS':True,'ERA HTTPS':{'tls':True,'status':404,'expected':404}}
    groups['onec']['data']={'postgres_tcp':True}
    groups['vpn']['data']={'interfaces':1}
    groups['backups']['data']={'published_at':9000,'vms':[{'vmid':100,'completed_at':9000,'identity_matches':True}],
                               'jobs':[{'enabled':True}]}
    return {'groups':groups,'windows_backup':{'Status':'success','ServerGeneratedAt':9000}},now


class OverviewContracts(unittest.TestCase):
    def frame(self,data,now,width=127,height=26):
        return ui.overview(data,False,width,height,d,now)

    def test_overview_fits_half_screen_and_keeps_backups_visible(self):
        data,now=healthy();frame=self.frame(data,now)
        rows=frame.split('\r\n')
        self.assertEqual(len(rows),26)
        self.assertTrue(all(ui.cell_width(row)==127 for row in rows))
        self.assertIn('РЕЗЕРВНЫЕ КОПИИ',frame)
        self.assertIn('Windows D:',frame)
        self.assertNotIn('increase pane height',frame)
        self.assertIn('В НОРМЕ',frame)

    def test_failed_app_is_reflected_in_top_summary(self):
        data,now=healthy();data['groups']['apps']['data']['1C TCP']=False
        rows=self.frame(data,now).split('\r\n')
        self.assertIn('ЕСТЬ СБОЙ',rows[0]);self.assertIn('1С: сбой проверки',rows[1])

    def test_stale_smart_has_unknown_badge_and_summary_attention(self):
        data,now=healthy();data['groups']['smart']['received_at']=1
        frame=self.frame(data,now)
        disk_line=next(row for row in frame.split('\r\n') if 'sda' in row)
        self.assertIn('НЕТ ДАННЫХ',disk_line)
        self.assertIn('ВНИМАНИЕ',frame.split('\r\n')[0])

    def test_fresh_windows_copy_never_hides_old_vm_archives(self):
        data,now=healthy();now=200000
        for group in data['groups'].values():group['received_at']=now
        data['groups']['backups']['data']['vms'][0]['completed_at']=1
        frame=self.frame(data,now)
        self.assertIn('проверить бекапы VM',frame)

    def test_read_only_filesystem_is_critical(self):
        data,now=healthy();data['groups']['storage']['data']['filesystems'][0]['readonly']=True
        frame=self.frame(data,now)
        self.assertIn('ЕСТЬ СБОЙ',frame.split('\r\n')[0]);self.assertIn('ТОЛЬКО ЧТЕНИЕ',frame)

    def test_resizing_and_wide_unicode_never_overflow_terminal_cells(self):
        data,now=healthy();data=copy.deepcopy(data)
        data['groups']['fast']['data']['hostname']='測試'*50
        for width,height in [(80,15),(108,26),(127,26),(160,30),(30,7)]:
            rows=self.frame(data,now,width,height).split('\r\n')
            self.assertEqual(len(rows),height)
            self.assertTrue(all(ui.cell_width(row)==width for row in rows),(width,height))


if __name__=='__main__':unittest.main()
