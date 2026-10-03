"""Launcher instances have independent paths and process lifetimes.

No user browser, profile, login or network connection is touched. The node
boundary is a process stub; real card rendering has its separate browser run.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name == 'nt' or not shutil.which('bash'), reason='POSIX launcher')
def test_two_instances_keep_profiles_outputs_and_lifetimes_separate(tmp_path):
    root = tmp_path/'project'
    script = root/'scripts'/'playwright-mcp.sh'
    script.parent.mkdir(parents=True)
    script.write_bytes((Path(__file__).resolve().parents[1]/'scripts'/'playwright-mcp.sh').read_bytes())
    cli = root/'.ha-dev/playwright-runtime/node_modules/@playwright/mcp/cli.js'
    cli.parent.mkdir(parents=True)
    cli.touch()
    binary = tmp_path/'bin'/'node'
    binary.parent.mkdir()
    binary.write_text(f'#!{sys.executable}\n' + '''import json, sys, time
if sys.argv[1] == '-e':
    print('/synthetic/chromium')
else:
    print(json.dumps(sys.argv[1:]), flush=True)
    while True:
        time.sleep(.1)
''')
    binary.chmod(0o755)
    env = {**os.environ, 'PATH': str(binary.parent)+os.pathsep+os.environ['PATH'],
           'DISPLAY': ':synthetic', 'WAYLAND_DISPLAY': ''}
    processes = []
    try:
        arguments = []
        for instance in ('review-a', 'review-b'):
            process = subprocess.Popen(['bash', str(script)], env={**env, 'BSF_PLAYWRIGHT_INSTANCE': instance},
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(process)
            arguments.append(json.loads(process.stdout.readline()))
        def option(args, name):
            return args[args.index(name)+1]
        assert option(arguments[0], '--user-data-dir') != option(arguments[1], '--user-data-dir')
        assert option(arguments[0], '--output-dir') != option(arguments[1], '--output-dir')
        assert option(arguments[0], '--user-data-dir') == str(root/'.ha-dev/playwright-profile-review-a')
        processes[0].terminate()
        processes[0].wait(timeout=5)
        assert processes[1].poll() is None
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
    invalid = subprocess.run(['bash', str(script)], env={**env, 'BSF_PLAYWRIGHT_INSTANCE': '../other'},
                             capture_output=True, text=True, timeout=5)
    assert invalid.returncode == 1
    assert 'BSF_PLAYWRIGHT_INSTANCE' in invalid.stderr
