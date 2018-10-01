#!/usr/bin/python2.7
# -*- coding: utf-8 -*-
#
# Copyright 2016 Unicode Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function, unicode_literals
import argparse, datetime, itertools, os, re, signal, subprocess
import threading, time
import xml.etree.ElementTree as etree
import svgutil


FONTTEST_NAMESPACE = '{https://github.com/OpenType/fonttest}'
FONTTEST_ID = FONTTEST_NAMESPACE + 'id'
FONTTEST_FONT = FONTTEST_NAMESPACE + 'font'
FONTTEST_RENDER = FONTTEST_NAMESPACE + 'render'
FONTTEST_VARIATION = FONTTEST_NAMESPACE + 'var'


class ConformanceChecker:
    def __init__(self, engine):
        self.engine = engine
        if self.engine == 'OpenType.js':
            self.command = 'node_modules/opentype.js/bin/test-render'
        elif self.engine == 'fontkit':
            self.command = 'src/third_party/fontkit/render'
        else:
            self.command = 'build/out/Default/fonttest'
        self.datestr = self.make_datestr()
        self.reports = {}  # filename --> HTML ElementTree
        self.conformance = {}  # testcase -> True|False
        self.observed = {}  # testcase --> SVG ElementTree

    def make_datestr(self):
        now = datetime.datetime.now()
        return '%s %d, %d' % (time.strftime("%B"), now.day, now.year)

    def make_command(self, e):
        testcase = e.attrib[FONTTEST_ID]
        font = os.path.join('fonts', e.attrib[FONTTEST_FONT])
        render = e.attrib.get(FONTTEST_RENDER)
        variation = e.attrib.get(FONTTEST_VARIATION)
        command = [self.command, '--font=' + font,
                   '--testcase=' + testcase, '--engine=' + self.engine]
        if render: command.append('--render=' + render)
        if variation: command.append('--variation=' + variation)
        return command

    def check(self, testfile):
        doc = etree.parse(testfile).getroot()
        self.reports[testfile] = doc
        all_ok = True
        for e in doc.findall(".//*[@class='expected']"):
            testcase = e.attrib[FONTTEST_ID]
            expected_svg = e.find('svg')
            self.normalize_svg(expected_svg)
            command = self.make_command(e)
            status, observed, _stderr = run_command(command, timeout_sec=3)
            if status == 0:
                observed = re.sub(r'>\s+<', '><', observed)
                observed = observed.replace(
                    'xmlns="http://www.w3.org/2000/svg"', '')
                observed_svg = etree.fromstring(observed)
                self.normalize_svg(observed_svg)
                ok = svgutil.is_similar(expected_svg, observed_svg,
                                        maxDelta=1.0)
                self.add_prefix_to_svg_ids(observed_svg, 'OBSERVED')
                self.observed[testcase] = observed_svg
            else:
                self.observed[testcase] = etree.fromstring(
                    '<div>&#x2053;</div>')
                ok = False
            self.conformance[testcase] = ok
            if not ok: all_ok = False
            print('%s %s' % ('PASS' if ok else 'FAIL', testcase))
        for e in doc.findall(".//*[@class='should-not-crash']"):
            testcase = e.attrib[FONTTEST_ID]
            command = self.make_command(e)
            status, _observed, _stderr = run_command(command, timeout_sec=3)
            ok = self.conformance[testcase] = (status == 0)
            if not ok: all_ok = False
            print('%s %s' % ('PASS' if ok else 'FAIL', testcase))
        for testcase, ok in list(self.conformance.items()):
            groups = testcase.split('/')
            for i in range(len(groups)):
                group = '/'.join(groups[:i])
                self.conformance[group] = (ok and
                                           self.conformance.get(group, True))

    def normalize_svg(self, svg):
        strip_path = lambda p: re.sub(r'\s+', ' ', p).strip()
        for path in svg.findall('.//path[@d]'):
            path.attrib['d'] = strip_path(path.attrib['d'])

    def add_prefix_to_svg_ids(self, svg, prefix):
        # The 'id' attribute needs to be globally unique in the HTML document,
        # so we add a prefix to distinguish identifiers in the expected versus
        # observed SVG image.
        for symbol in svg.findall('.//symbol[@id]'):
            symbol.attrib['id'] = '%s/%s' % (prefix, symbol.attrib['id'])
        href = '{http://www.w3.org/1999/xlink}href'
        for use in svg.findall('.//use[@%s]' % href):
            assert use.attrib[href][0] == '#', use.attrib[href]
            use.attrib[href] = '#%s/%s' % (prefix, use.attrib[href][1:])

    def write_report(self, path):
        report = etree.parse('testcases/index.html').getroot()
        report.find("./body/h2").text = self.datestr + ' · ' + self.engine
        summary = report.find("./body//*[@id='SummaryText']")
        fails = [k for k, v in self.conformance.items() if k and not v]
        fails = sorted(set([t.split('/')[0] for t in fails]), key=sortkey)
        if len(fails) == 0:
            summary.text = 'All tests have passed.'
        else:
            summary.text = 'Some tests have failed. For details, see '
            for f in fails:
                if f is not fails[0]:
                    if f is fails[-1]:
                        etree.SubElement(summary, None).text = ', and '
                    else:
                        etree.SubElement(summary, None).text = ', '
                link = etree.SubElement(summary, 'a')
                link.text, link.attrib['href'] = f, '#' + f
            etree.SubElement(summary, None).text = '.'

        head = report.find("./head")
        for sheet in list(head.findall("./link[@rel='stylesheet']")):
            href = sheet.attrib.get('href')
            if href and '://' not in href:
                internalStyle = etree.SubElement(head, 'style')
                with open(os.path.join('testcases', href), 'r') as sheetfile:
                    internalStyle.text = sheetfile.read().decode('utf-8')
                head.remove(sheet)

        for filename in sorted(self.reports.keys(), key=sortkey):
            doc = self.reports[filename]
            for e in doc.findall(".//*[@class='observed']"):
                e.append(self.observed.get(e.attrib[FONTTEST_ID]))
            conf = (list(doc.findall(".//*[@class='conformance']")) +
                    list(doc.findall(".//*[@class='should-not-crash']")))
            for e in conf:
                if self.conformance.get(e.attrib[FONTTEST_ID]):
                    e.text, e.attrib['class'] = '✓', 'conformance-pass'
                else:
                    e.text, e.attrib['class'] = '✖', 'conformance-fail'

            for subElement in doc.find('body'):
                report.find('body').append(subElement)

        with open(path, 'w') as outfile:
            xml = etree.tostring(report, encoding='utf-8')
            xml = xml.replace(b'svg:', b'')  # work around browser bugs
            outfile.write(xml)


def sortkey(s):
    """'tests/GVAR-10B.html' --> 'tests/GVAR-0000000010B.html'"""
    return re.sub(r'\d+', lambda match: '%09d' % int(match.group(0)), s)


def run_command(cmd, timeout_sec):
    child = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    timer = threading.Timer(timeout_sec, child.kill)
    try:
        timer.start()
        stdout, stderr = child.communicate()
    finally:
        timer.cancel()
    return child.returncode, stdout, stderr


def build(engine):
    if engine == 'OpenType.js' or engine == 'fontkit':
        subprocess.check_call(['npm', 'install'])
    else:
        subprocess.check_call(
            './src/third_party/gyp/gyp -f make --depth . '
            '--generator-output build  src/fonttest/fonttest.gyp'.split())
        subprocess.check_call(['make', '-s', '--directory', 'build'])


def main():
    etree.register_namespace('svg', 'http://www.w3.org/2000/svg')
    etree.register_namespace('xlink', 'http://www.w3.org/1999/xlink')
    parser = argparse.ArgumentParser()
    parser.add_argument('--engine',
                        choices=['FreeStack', 'CoreText', 'DirectWrite', 'OpenType.js', 'fontkit'],
                        default='FreeStack')
    parser.add_argument('--output', help='path to report file being written')
    args = parser.parse_args()
    build(engine=args.engine)
    checker = ConformanceChecker(engine=args.engine)
    for filename in sorted(os.listdir('testcases'), key=sortkey):
        if (filename == 'index.html'
                or not filename.endswith('.html')):
            continue
        checker.check(os.path.join('testcases', filename))
    print('PASS' if checker.conformance.get('') else 'FAIL')
    if args.output:
        checker.write_report(args.output)


if __name__ == '__main__':
    main()
