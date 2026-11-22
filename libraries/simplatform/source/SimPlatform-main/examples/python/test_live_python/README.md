# Live Python Control verification

Run the complete scene task without a window:

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
python examples\python\test_live_python\jaka_wsg50_live.py --kind headless
```

Launch the GUI and keep opening and closing the WSG50 every 0.5 seconds until the
editor window is closed:

```powershell
python examples\python\test_live_python\jaka_wsg50_live.py --kind gui
```

Use `--cycles N` for a finite number of gripper commands. The default is six
commands in headless mode and unlimited commands in GUI mode.

Command the JAKA in an existing Live session, with angles specified in degrees:

```powershell
python examples\python\test_live_python\set_jaka_joint_angles_live.py 10 -20 30 40 50 60
```

Passing one angle applies it to all six joints:

```powershell
python examples\python\test_live_python\set_jaka_joint_angles_live.py 60
```

The script discovers existing sessions and prefers `live-python-jaka-gui`. Use
`--session NAME`, `--session pid:1234`, or `--session tcp://127.0.0.1:8765` to
select one explicitly. It exits without changing anything when the session or
the `robot` JAKA alias is unavailable.
