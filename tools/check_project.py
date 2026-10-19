"""Static checks of the packaged native Ignition export, tags and source links.

No Gateway, imports into Ignition, network calls, or equipment writes.
"""
import ast
import hashlib
import json
from pathlib import Path
import re
import textwrap
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
CHECKS = []


def check(label, condition):
    CHECKS.append({'check': label, 'passed': bool(condition)})
    if not condition:
        raise AssertionError(label)


def walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def security_paths(permission):
    def leaves(nodes, prefix=''):
        result = []
        for node in nodes:
            name = prefix + node['name']
            children = node.get('children', [])
            result.extend(leaves(children, name + '/') if children else [name])
        return result
    return leaves(permission.get('securityLevels', []))


def main():
    archive = ROOT / 'ignition/SubstationSCADA_project.zip'
    with ZipFile(archive) as z:
        check('Native project ZIP CRC is valid', z.testzip() is None)
        names = z.namelist()
        check('Native export contains safe unique resource names', len(set(names)) == len(names) and all(not Path(n).is_absolute() and '..' not in Path(n).parts for n in names))
        for name in names:
            check('Exploded resource matches native ZIP: ' + name,
                  (ROOT / 'ignition/project' / name).read_bytes() == z.read(name))
        views = {n.split('/views/')[1].split('/')[0]: json.loads(z.read(n)) for n in names if n.endswith('/view.json')}
        check('Exactly six expected Perspective views are included', set(views) == {'Overview','Controls','Alarms','Trends','AlarmHistory','CommandHistory'})
        pages = json.loads(z.read('com.inductiveautomation.perspective/page-config/config.json'))['pages']
        check('Homepage opens Overview', pages['/']['viewPath'] == 'Overview')
        check('All seven page mappings reference included views', len(pages) == 7 and all(p['viewPath'] in views for p in pages.values()))
        session_permission = json.loads(z.read('com.inductiveautomation.perspective/session-permissions/data.bin'))
        check('Session permission requires the Viewer level', session_permission['type'] == 'AllOf' and security_paths(session_permission) == ['Authenticated/Roles/Viewer'])
        timer = json.loads(z.read('ignition/timer/Phase8Health/resource.json'))['attributes']
        check('Health timer is enabled, dedicated and fixed-delay at one second', timer['enabled'] and timer['fixedDelay'] and not timer['sharedThread'] and timer['delay'] == 1000)
        check('Timer calls the current monitor module', z.read('ignition/timer/Phase8Health/handleTimerEvent.py').decode().strip().endswith('phase8_monitor.tick()'))
        for name in ['phase7_controls','phase8_monitor']:
            code = z.read('ignition/script-python/' + name + '/code.py')
            check('Readable module matches native export: ' + name, code == (ROOT / 'ignition/scripts' / (name + '.py')).read_bytes())
            ast.parse(code.decode())
        control_code = ast.parse((ROOT / 'ignition/scripts/phase7_controls.py').read_text())
        assignments = {n.targets[0].id: n.value for n in control_code.body if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
        check('Command helper is version 11.1', ast.literal_eval(assignments['VERSION']) == '11.1')

    tags = {}
    def add_tags(node, prefix='[default]'):
        path = prefix + node['name']
        tags[path] = node
        for child in node.get('tags', []):
            add_tags(child, path + '/')
    add_tags(json.loads((ROOT / 'ignition/Substation_tags.json').read_text()))
    atomic = {p:t for p,t in tags.items() if t.get('tagType') == 'AtomicTag'}
    opc = {p:t for p,t in atomic.items() if t.get('valueSource') == 'opc'}
    check('Tag export contains 74 atomic tags and 41 OPC points', len(atomic) == 74 and len(opc) == 41)
    check('Twelve tags have history enabled', sum(bool(t.get('historyEnabled')) for t in atomic.values()) == 12)
    check('Eleven alarm definitions are included', sum(len(t.get('alarms', [])) for t in atomic.values()) == 11)
    points = json.loads((ROOT / 'simulator/config/points.json').read_text())['points']
    check('Simulator map has exactly 41 unique points', len(points) == len({p['signal'] for p in points}) == 41)
    check('OPC tag item paths match the simulator point map', {p['opc_item_path'] for p in points} == {t['opcItemPath'] for t in opc.values()})
    config = json.loads((ROOT / 'simulator/config/substation.json').read_text())
    check('Simulator endpoint stays at localhost port 1502, unit 1, zero-based', config['modbus']['host'] == '127.0.0.1' and config['modbus']['port'] == 1502 and config['modbus']['device_id'] == 1 and config['modbus']['zero_based_addressing'])

    references, nav, protected, scripts, bidirectional = set(), set(), [], [], []
    for name, view in views.items():
        for value in walk(view):
            if isinstance(value, str):
                references.update(re.findall(r'\[default\]Substation/[A-Za-z0-9_/]+', value))
            if not isinstance(value, dict):
                continue
            if value.get('type') == 'tag' and value.get('config', {}).get('bidirectional'):
                bidirectional.append((name, value))
            if value.get('type') == 'script' and 'script' in value.get('config', {}):
                script = value['config']['script']
                scripts.append(script)
                tree = ast.parse('def handler(self, event):\n' + textwrap.indent(textwrap.dedent(script).strip() or 'pass', '    ') + '\n')
                for node in ast.walk(tree):
                    if isinstance(node, ast.keyword) and node.arg == 'page' and isinstance(node.value, ast.Constant):
                        nav.add(node.value.value)
                if 'phase7_controls.send_command' in script or 'phase7_controls.set_simulation' in script:
                    permission = value.get('permissions', {})
                    protected.append((name, permission.get('type') == 'AllOf' and security_paths(permission) == ['Authenticated/Roles/Operator']))
                if 'system.perspective.logout()' in script:
                    check('Sign Out is available to the Viewer session level', security_paths(value.get('permissions', {})) == ['Authenticated/Roles/Viewer'])
            if value.get('type') == 'script' and 'code' in value:
                ast.parse('def transform(self, value, quality, timestamp):\n' + textwrap.indent(textwrap.dedent(value['code']).strip(), '    ') + '\n')
    check('Every static view tag reference resolves', references <= set(tags))
    check('Every navigation destination exists', nav <= set(pages))
    check('All nine equipment/scenario actions require Operator', len(protected) == 9 and all(name == 'Controls' and ok for name, ok in protected))
    check('No view has a bidirectional tag binding', not bidirectional)
    expected_overview = {'sldUsable','sldGridState','sldCB1Position','sldCB1Tripped','sldCB1State','sldBusState','sldPVState','sldModelState','sldGridColor','sldBusColor','sldCB1Color','sldPVColor'}
    check('Overview has all twelve live-state bindings', set(views['Overview']['custom']) == expected_overview and set(views['Overview']['propConfig']) == {'custom.' + name for name in expected_overview})
    check('Overview includes no command or tag-write calls', not re.search(r'send_command|set_simulation|writeBlocking|system\.tag\.write', json.dumps(views['Overview'])))
    chart = next(n for n in walk(views['Trends']) if isinstance(n,dict) and n.get('type') == 'ia.chart.powerchart')['props']
    check('Trends has the two Grid/PV pens', [p['name'] for p in chart['pens']] == ['Grid_P_kW','PV_AC_Power_kW'])
    check('Trends has ten-minute range, two-second refresh and no historical mode override', chart['config']['unitOfTime'] == 10 and chart['config']['measureOfTime'] == 'minutes' and chart['config']['refreshRate'] == 2000 and chart['config'].get('mode') != 'historical')
    journal = next(n for n in walk(views['AlarmHistory']) if isinstance(n,dict) and n.get('type') == 'ia.display.alarmjournaltable')
    check('AlarmHistory references the working database journal', journal['props']['name'] == 'Sample_Alarm_Journal_Profile')
    monitor_ast = ast.parse((ROOT / 'ignition/scripts/phase8_monitor.py').read_text())
    names = {n.targets[0].id: ast.literal_eval(n.value) for n in monitor_ast.body if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and n.targets[0].id in ['ANALOG_NAMES','STATUS_NAMES','DIAGNOSTIC_NAMES','INPUT_NAMES']}
    expected_read_paths = ['[default]Substation/' + folder + '/' + name for folder, group in [('Raw',names['ANALOG_NAMES']),('Measurements',names['ANALOG_NAMES']),('Status',names['STATUS_NAMES']),('Diagnostics',names['DIAGNOSTIC_NAMES']),('Inputs',names['INPUT_NAMES']),('Commands',('Command_Code','Command_Sequence'))] for name in group]
    check('All 59 monitor read paths resolve to exported tags', len(expected_read_paths) == 59 and set(expected_read_paths) <= set(atomic))
    for path in ROOT.rglob('*.md'):
        for target in re.findall(r'\]\(([^)]+)\)', path.read_text()):
            if '://' in target or target.startswith('#'):
                continue
            check('Relative Markdown link resolves: ' + path.relative_to(ROOT).as_posix() + ' -> ' + target, (path.parent / target.split('#')[0]).exists())
    report = {'scope':'Static export/tag/source consistency checks; no live Ignition execution', 'passed':True,
              'checks_passed':len(CHECKS),'script_actions_checked':len(scripts),'view_tag_references_checked':len(references),'checks':CHECKS}
    (ROOT / 'validation/project_static_checks.json').write_text(json.dumps(report,indent=2) + '\n')
    print('PROJECT STATIC CHECKS: PASS (%d)' % len(CHECKS))


if __name__ == '__main__':
    main()
