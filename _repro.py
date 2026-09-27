import sys; sys.path.insert(0, 'src')
from py_agent.plugins import PluginRuntime, Contributions, PluginManifest, hookimpl, Hooks
import pluggy
class Invalid:
    @hookimpl
    def py_agent_register(self):
        return Contributions(PluginManifest('invalid'))
    @hookimpl(optionalhook=True)
    def py_agent_unrecognized(self):
        return None
try:
    PluginRuntime.load(builtins={'invalid': Invalid()})
    print('LOADED WITHOUT ERROR')
except Exception as exc:
    print('RAISED', type(exc).__name__, str(exc)[:120])
pm = pluggy.PluginManager('ipy_agent'); pm.add_hookspecs(Hooks)
for attr in dir(Invalid):
    opts = pm.parse_hookimpl_opts(Invalid, attr)
    if opts is not None:
        print('impl found:', attr, 'known=', hasattr(pm.hook, attr), opts)