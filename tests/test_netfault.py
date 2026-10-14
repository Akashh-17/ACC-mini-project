"""Exercise delay lifecycle with fake Linux tools; never change host networking."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


MOCK_TC = r'''#!/usr/bin/env bash
set -eu
kind=$1
action=$2
shift 2
handle=''
destination=''
has_root_arg=false
previous=''
for arg in "$@"; do
  if [[ "$previous" == handle ]]; then handle=$arg; fi
  if [[ "$previous" == dst_ip ]]; then destination=$arg; fi
  if [[ "$arg" == root ]]; then has_root_arg=true; fi
  previous=$arg
done
case "$kind:$action" in
  qdisc:show)
    if [[ -f "$FAKE_STATE/root" ]]; then echo 'qdisc prio 1: root'; fi
    ;;
  qdisc:replace)
    if $has_root_arg; then
      : > "$FAKE_STATE/root"
      : > "$FAKE_STATE/filters"
    fi
    ;;
  qdisc:del)
    rm -f "$FAKE_STATE/root"
    : > "$FAKE_STATE/filters"
    ;;
  filter:replace)
    awk -v h="$handle" '$1 != h' "$FAKE_STATE/filters" > "$FAKE_STATE/new"
    printf '%s %s\n' "$handle" "$destination" >> "$FAKE_STATE/new"
    mv "$FAKE_STATE/new" "$FAKE_STATE/filters"
    ;;
  filter:del)
    awk -v h="$handle" '$1 != h' "$FAKE_STATE/filters" > "$FAKE_STATE/new"
    mv "$FAKE_STATE/new" "$FAKE_STATE/filters"
    ;;
  filter:show)
    cat "$FAKE_STATE/filters"
    ;;
  *) echo "unsupported fake tc operation" >&2; exit 1 ;;
esac
'''


def bash_binary():
    if os.name == 'nt':
        candidate = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/bin/bash.exe'
        return str(candidate) if candidate.exists() else None
    return shutil.which('bash')


@unittest.skipUnless(bash_binary(), 'Bash required for Linux tool mocks')
class NetfaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.bin = self.folder / 'bin'
        self.bin.mkdir()
        self.state = self.folder / 'state'
        self.state.mkdir()
        (self.state / 'filters').write_text('')
        mocks = {'tc': MOCK_TC, 'ip': '#!/usr/bin/env bash\necho "192.0.2.1 dev test0"\n',
                 'iptables': '#!/usr/bin/env bash\nexit 0\n'}
        for name, text in mocks.items():
            path = self.bin / name
            path.write_text(text, encoding='utf-8', newline='\n')
            path.chmod(0o755)
        self.script = Path(__file__).resolve().parents[1] / 'scripts/netfault.sh'

    def tearDown(self):
        self.temp.cleanup()

    def call(self, action, peer, delay=None):
        env = dict(os.environ, FAKE_STATE=self.state.as_posix(), MOCK_BIN=self.bin.as_posix(),
                   FAULT_SCRIPT=self.script.as_posix(), FAULT_ACTION=action, FAULT_PEER=peer,
                   FAULT_DELAY=str(delay) if delay is not None else '')
        # No paths are interpolated into the shell command; values are env vars.
        command = ('if command -v cygpath >/dev/null; then '
                   'MOCK_BIN=$(cygpath -u "$MOCK_BIN"); FAKE_STATE=$(cygpath -u "$FAKE_STATE"); fi; '
                   'export FAKE_STATE; export PATH="$MOCK_BIN:$PATH"; '
                   'bash "$FAULT_SCRIPT" "$FAULT_ACTION" "$FAULT_PEER" ${FAULT_DELAY:+"$FAULT_DELAY"}')
        return subprocess.run([bash_binary(), '-c', command], env=env, capture_output=True, text=True)

    def test_second_peer_delay_preserves_first_and_clear_is_per_peer(self):
        for peer in ('192.0.2.1', '192.0.2.2'):
            result = self.call('delay', peer, 500)
            self.assertEqual(result.returncode, 0, result.stderr)
        filters = (self.state / 'filters').read_text()
        self.assertIn('192.0.2.1/32', filters)
        self.assertIn('192.0.2.2/32', filters)
        # Reapplying one peer at a new delay must not duplicate/drop peer filters.
        result = self.call('delay', '192.0.2.1', 1000)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len((self.state / 'filters').read_text().splitlines()), 2)
        result = self.call('clear', '192.0.2.1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.state / 'root').exists())
        self.assertNotIn('192.0.2.1/32', (self.state / 'filters').read_text())
        self.assertIn('192.0.2.2/32', (self.state / 'filters').read_text())
        result = self.call('clear', '192.0.2.2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.state / 'root').exists())

    def test_invalid_address_or_delay_does_not_apply_delay(self):
        for peer, delay in (('999.0.2.1', 500), ('192.0.2.1', '-1')):
            result = self.call('delay', peer, delay)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.state / 'root').exists())
