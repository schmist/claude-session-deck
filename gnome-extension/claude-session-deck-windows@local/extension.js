import Gio from 'gi://Gio';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const IFACE = `
<node>
  <interface name="ch.claudesessiondeck.Windows">
    <method name="List"><arg type="s" direction="out"/></method>
    <method name="Activate">
      <arg type="u" direction="in"/>
      <arg type="b" direction="out"/>
    </method>
  </interface>
</node>`;

export default class ClaudeSessionDeckWindows extends Extension {
    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, '/ch/claudesessiondeck/Windows');
    }

    disable() {
        this._dbus.unexport();
        this._dbus = null;
    }

    _windows() {
        return global.get_window_actors().map(a => a.meta_window);
    }

    List() {
        return JSON.stringify(this._windows().map(w => ({
            id: w.get_id(),
            pid: w.get_pid(),
            title: w.get_title(),
            wm_class: w.get_wm_class(),
            focus: w.has_focus(),
        })));
    }

    Activate(id) {
        const win = this._windows().find(w => w.get_id() === id);
        if (!win)
            return false;
        Main.activateWindow(win);
        return true;
    }
}
