import os
import threading
import uuid

import cherrypy
from ipaddress import IPv4Interface
from jinja2 import Environment
from jinja2 import FileSystemLoader
import netifaces


current_dir = os.path.dirname(os.path.abspath(__file__))
template_env = None


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

    def run(self):
        action = 'poweroff'
        if self._restart:
            action = 'reboot'
        os.system('sudo /bin/systemctl {}'.format(action))

    def delay_start(self):
        if self._thread and self._thread.is_alive():
            raise cherrypy.HTTPError(429, 'Too Many Requests')
        self._thread = threading.Timer(self._time_delay, self.run)
        self._thread.start()

    def stop(self):
        if self._thread and self._thread.is_alive():
            self._thread.cancel()
        self._thread = None

    def is_alive(self):
        return self._thread.is_alive()


class Server(object):
    def __init__(self):
        self.uuid = str(uuid.uuid4())
        self.poweroff_task = None

        self.template_env = None
        cherrypy.config.namespaces['template'] = self._template_namespace

    def _addresses(self):
        addrs = []
        for iface in netifaces.interfaces():
            ip4addrs = []
            for inet in netifaces.ifaddresses(iface)[netifaces.AF_INET]:
                ip4addrs.append(IPv4Interface(
                    '{addr}/{netmask}'.format(**inet)).with_prefixlen)
            addrs.append({'interface': iface, 'ipv4': ','.join(ip4addrs)})
        return addrs

    def _template_namespace(self, k, v):
        self.template_env = Environment(loader=FileSystemLoader(v))

    @cherrypy.expose
    def index(self):
        template = self.template_env.get_template('index.html')
        return template.render(
            nodename=os.uname().nodename,
            uuid=self.uuid,
            addrs=self._addresses(),
        )

    def _poweroff_start(self, id, restart):
        if id != self.uuid:
            raise cherrypy.HTTPError('422 Unprocessable Entity')
        cherrypy.engine.poweroff.restart = restart
        cherrypy.engine.poweroff.delay_start()

    @cherrypy.expose
    def poweroff(self, id=None):
        self._poweroff_start(id=id, restart=False)
        cherrypy.response.status = '202 Accepted'
        template = self.template_env.get_template('poweroff.html')
        return template.render(
            nodename=os.uname().nodename,
        )

    @cherrypy.expose
    def restart(self, id=None):
        self._poweroff_start(id=id, restart=True)
        cherrypy.response.status = '202 Accepted'
        template = self.template_env.get_template('restart.html')
        return template.render(
            nodename=os.uname().nodename,
        )


cherrypy.engine.poweroff = Poweroff(cherrypy.engine)
cherrypy.engine.poweroff.subscribe()

cherrypy.engine.drop_privileges = \
    cherrypy.process.plugins.DropPrivileges(cherrypy.engine)
cherrypy.engine.drop_privileges.subscribe()


if __name__ == '__main__':
    config = os.path.join(current_dir, 'server.conf')
    cherrypy.quickstart(Server(), config=config)
