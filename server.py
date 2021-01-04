import os
import sys
import threading

import cherrypy
from ipaddress import IPv4Interface
from jinja2 import Environment
from jinja2 import FileSystemLoader
import netifaces

import dbus
import dbus.mainloop.glib
from gi.repository import GLib


current_dir = os.path.dirname(os.path.abspath(__file__))


class BlueZDbus(cherrypy.process.plugins.SimplePlugin):
    def __init__(self, bus):
        cherrypy.process.plugins.SimplePlugin.__init__(self, bus)

        # Get the system bus
        try:
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            self._system_bus = dbus.SystemBus()
        except Exception as ex:
            cherrypy.log(
                'Unable to get the system dbus: "{}".'.format(ex.message))
            cherrypy.log('Exiting. Is dbus running?')
            sys.exit(1)

        self._mainloop = GLib.MainLoop()

        self.adapter_path = None
        self.adapter = None

    class Adapter:
        def __init__(self, system_bus, path):
            self._interface = dbus.Interface(
                system_bus.get_object('org.bluez', path),
                'org.freedesktop.DBus.Properties')

        @property
        def address(self):
            val = self._interface.Get('org.bluez.Adapter1', 'Address')
            return val

        @property
        def powered(self):
            _val = self._interface.Get('org.bluez.Adapter1', 'Powered')
            return _val == dbus.Boolean(True)

        @powered.setter
        def powered(self, val):
            _val = dbus.Boolean(val)
            self._interface.Set('org.bluez.Adapter1', 'Powered', _val)

    class Device:
        def __init__(self, system_bus):
            self._manager = dbus.Interface(
                system_bus.get_object('org.bluez', '/'),
                'org.freedesktop.DBus.ObjectManager')
            self._system_bus = system_bus

            self._system_bus.add_signal_receiver(
                self._interfaces_added,
                dbus_interface='org.freedesktop.DBus.ObjectManager',
                signal_name='InterfacesAdded')

            self._system_bus.add_signal_receiver(
                self._properties_changed,
                dbus_interface='org.freedesktop.DBus.Properties',
                signal_name='PropertiesChanged',
                arg0='org.bluez.Device1',
                path_keyword='path')

        def _print_properties(self, path, properties):
            cherrypy.log('Path {}'.format(path))
            for key in properties.keys():
                value = properties[key]
                if (key == 'Class'):
                    cherrypy.log('  %s = 0x%06x' % (key, value))
                else:
                    cherrypy.log('  %s = %s' % (key, value))

        def devices(self):
            cherrypy.log('Devices')
            _devices = []
            obj = self._manager.GetManagedObjects()
            for path, interface in obj.items():
                if 'org.bluez.Device1' not in interface:
                    continue
                properties = interface['org.bluez.Device1']
                self._print_properties(path, properties)
                _device = {
                    'Name': properties['Name'],
                    'Address': properties['Address'],
                    'Connected': properties['Connected'],
                    'path': path,
                }
                _devices.append(_device)
            return _devices

        def _interfaces_added(self, path, interfaces):
            if 'org.bluez.Device1' not in interfaces:
                return
            properties = interfaces['org.bluez.Device1']
            cherrypy.log('Interfaces added')
            self._print_properties(path, properties)

        def _properties_changed(self, interface, changed, invalidated, path):
            if interface != 'org.bluez.Device1':
                return
            cherrypy.log('Properties changed')
            self._print_properties(path, changed)

        def connect(self, path):
            cherrypy.log('Connect to {}'.format(path))
            interface = dbus.Interface(
                self._system_bus.get_object('org.bluez', path),
                'org.bluez.Device1')
            interface.Connect()

        def disconnect(self, path):
            cherrypy.log('Disconnect {}'.format(path))
            interface = dbus.Interface(
                self._system_bus.get_object('org.bluez', path),
                'org.bluez.Device1')
            interface.Disconnect()

    def _run(self):
        cherrypy.log('Dbus mainloop.run')
        try:
            self._mainloop.run()
        except KeyboardInterrupt:
            pass

    def start(self):
        if self.adapter is None:
            self.adapter = BlueZDbus.Adapter(
                self._system_bus, self.adapter_path)
            self.device = BlueZDbus.Device(self._system_bus)
        self._thread = threading.Thread(target=self._run)
        self._thread.start()

    def stop(self):
        self._mainloop.quit()
        cherrypy.log('Dbus mainloop.quit')


class Poweroff(cherrypy.process.plugins.SimplePlugin):
    def __init__(self, bus):
        cherrypy.process.plugins.SimplePlugin.__init__(self, bus)
        self._thread = None
        self._restart = False
        self._time_delay = 10

    @property
    def restart(self):
        return self._restart

    @restart.setter
    def restart(self, val):
        self._restart = val

    @property
    def time_delay(self):
        return self._time_delay

    @time_delay.setter
    def time_delay(self, val):
        self._time_delay = val

    def _run(self):
        action = 'poweroff'
        if self._restart:
            action = 'reboot'
        os.system('sudo /bin/systemctl {}'.format(action))

    def delay_start(self):
        if self._thread and self._thread.is_alive():
            raise cherrypy.HTTPError(429, 'Too Many Requests')
        self._thread = threading.Timer(self._time_delay, self._run)
        self._thread.start()

    def stop(self):
        if self._thread and self._thread.is_alive():
            self._thread.cancel()
        self._thread = None

    def is_alive(self):
        return self._thread.is_alive()


class Template:
    def __init__(self):
        self.env = None
        cherrypy.config.namespaces['template'] = self._namespace

    def _namespace(self, k, v):
        if k == 'dir':
            self.env = Environment(loader=FileSystemLoader(v))


class Root:
    def __init__(self, template):
        self._template = template
        self._bluez = cherrypy.engine.bluez_dbus

    def _addresses(self):
        addrs = []
        for iface in netifaces.interfaces():
            ip4addrs = []
            for inet in netifaces.ifaddresses(iface)[netifaces.AF_INET]:
                ip4addrs.append(IPv4Interface(
                    '{addr}/{netmask}'.format(**inet)).with_prefixlen)
            addrs.append({'interface': iface, 'ipv4': ','.join(ip4addrs)})
        return addrs

    @cherrypy.expose
    def index(self):
        return self._template.env.get_template(
            'index.html'
        ).render(
            nodename=os.uname().nodename,
            addrs=self._addresses(),
            bluetooth_powered=self._bluez.adapter.powered,
        )

    def _poweroff(self, restart):
        cherrypy.engine.poweroff.restart = restart
        cherrypy.engine.poweroff.delay_start()
        return self._template.env.get_template(
            'restart.html' if restart else 'poweroff.html',
        ).render(
            nodename=os.uname().nodename,
        )

    @cherrypy.expose
    def poweroff(self):
        return self._poweroff(restart=False)

    @cherrypy.expose
    def restart(self):
        return self._poweroff(restart=True)


class Bluetooth:
    def __init__(self, template):
        self._template = template
        self._bluez = cherrypy.engine.bluez_dbus

    @cherrypy.expose
    def index(self):
        return self._template.env.get_template(
            'bluetooth/index.html'
        ).render(
            nodename=os.uname().nodename,
            powered=self._bluez.adapter.powered,
            devices=self._bluez.device.devices(),
        )

    @cherrypy.expose
    def power(self, on_off=None):
        if on_off == 'on':
            val = True
        elif on_off == 'off':
            val = False
        else:
            raise cherrypy.HTTPError('400 Bad Request')
        self._bluez.adapter.powered = val
        return self._template.env.get_template(
            'bluetooth/power.html'
        ).render(
            on_off=on_off,
        )

    @cherrypy.expose
    def connect(self, address=None):
        for device in self._bluez.device.devices():
            if address != device['Address']:
                continue
            self._bluez.device.connect(device['path'])
            raise cherrypy.HTTPRedirect('/bluetooth')
        raise cherrypy.NotFound()

    @cherrypy.expose
    def disconnect(self, address=None):
        for device in self._bluez.device.devices():
            if address != device['Address']:
                continue
            self._bluez.device.disconnect(device['path'])
            raise cherrypy.HTTPRedirect('/bluetooth')
        raise cherrypy.NotFound()


if __name__ == '__main__':
    cherrypy.engine.drop_privileges = \
        cherrypy.process.plugins.DropPrivileges(cherrypy.engine)
    cherrypy.engine.bluez_dbus = BlueZDbus(cherrypy.engine)
    cherrypy.engine.poweroff = Poweroff(cherrypy.engine)

    template = Template()
    config = os.path.join(current_dir, 'server.conf')
    cherrypy.config.update(config)
    cherrypy.tree.mount(Root(template), '/', config)
    cherrypy.tree.mount(Bluetooth(template), '/bluetooth', config)

    cherrypy.engine.start()
    cherrypy.engine.block()
