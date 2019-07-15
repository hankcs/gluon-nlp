# -*- coding:utf-8 -*-
# Author: hankcs
# Date: 2019-06-21 11:40

with open('scripts/data/test.tsv') as test, open('scripts/output_dir/RAD.csv') as predict, open(
        'scripts/output_dir/diff.csv', 'w') as out:
    out.write('{}\t{}\t{}\n'.format('report', 'gold', 'predict'))
    predict.readline()
    for gold, pred in zip(test, predict):
        g = gold.strip().split()[-1]
        p = pred.strip().split()[-1]
        if g != p:
            out.write('{}\t{}\n'.format(gold.strip(), p))

