#!/usr/bin/env python3
"""Offline suites for the canonical Persona source. Never load production configs."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT/'controller'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite',choices=('core','slack','all'),default='core')
    args = parser.parse_args()
    if not (SOURCE/'ctl.py').is_file():
        parser.error('Canonical controller source is not present')
    env = os.environ.copy()
    env['INTENT_TEST_GATE_PATH'] = str(SOURCE/'gate.py')
    env['CONTROLLER_CONTROLLER'] = str(SOURCE)
    suites = []
    if args.suite in ('core','all'):
        suites += [('controller/intents',['discover','-s','tests','-q']),
                   ('gate',['-q','test_gate']),('feed publisher',['-q','test_feed_publisher'])]
    if args.suite in ('slack','all'):
        suites.append(('Slack local fake API',['discover','-s','slack/tests','-q']))
    failed = False
    for label, command in suites:
        print(f'Running {label}',flush=True)
        try:
            result = subprocess.run([sys.executable,'-m','unittest',*command],
                                    cwd=SOURCE,env=env,
                                    timeout=180 if label == 'controller/intents' else 120,check=False)
            failed = failed or result.returncode != 0
        except subprocess.TimeoutExpired:
            print(f'{label}: timed out',file=sys.stderr,flush=True)
            failed = True
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
