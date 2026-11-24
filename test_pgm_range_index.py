import copy
import random
import unittest
from pgm_range_index import CoverageGroupIndex


def group(first,last,session,family='play'):
    return dict(first=first,last=last,family=family,takes=[dict(key=(session,str(first)))])


class CoverageRangeIndexTests(unittest.TestCase):
    def test_random_growth_repeated_sessions_partial_ranges_and_ambiguity_match_full_scan(self):
        rng=random.Random(20261014)
        groups=[];index=CoverageGroupIndex()
        for n in range(4000):
            first=rng.randrange(0,500)
            take=dict(first=first,last=first+rng.randrange(0,100),family=rng.choice(('one','two')),
                      key=(str(rng.randrange(6)),str(n)))
            expected=[g for g in groups if g['family']==take['family'] and
                not any(r['key'][0]==take['key'][0] for r in g['takes']) and
                (g['first']<=take['first']<=take['last']<=g['last'] or
                 take['first']<=g['first']<=g['last']<=take['last'])]
            actual=index.compatible(take)
            self.assertEqual(min(2,len(expected)),len(actual))
            if len(expected)==1:
                self.assertIs(actual[0],expected[0])
                chosen=actual[0]
                chosen['first']=min(chosen['first'],take['first'])
                chosen['last']=max(chosen['last'],take['last'])
                chosen['takes'].append(take)
                index.changed(chosen)
            else:
                chosen=dict(first=take['first'],last=take['last'],family=take['family'],takes=[take])
                groups.append(chosen);index.add(chosen)

    def test_same_session_thousands_of_repeated_takes_do_not_scan_each_group(self):
        index=CoverageGroupIndex()
        for n in range(10000):index.add(group(10,20,'same'))
        index.visited=0
        self.assertEqual(index.compatible(dict(first=11,last=19,family='play',key=('same','new'))),[])
        self.assertEqual(index.visited,1)
        self.assertEqual(len(index.compatible(dict(first=11,last=19,family='play',key=('other','new')))),2)

    def test_disjoint_ranges_prune_and_group_growth_retains_identity(self):
        index=CoverageGroupIndex()
        groups=[group(n*10,n*10+2,'same') for n in range(3000)]
        for row in groups:index.add(row)
        index.visited=0
        take=dict(first=15000,last=15001,family='play',key=('other','new'))
        self.assertEqual(index.compatible(take),[groups[1500]])
        self.assertLess(index.visited,100)
        groups[1500]['first']=14995;groups[1500]['last']=15005
        index.changed(groups[1500])
        take.update(first=14996,last=15004)
        self.assertEqual(index.compatible(take),[groups[1500]])


if __name__=='__main__':unittest.main()
