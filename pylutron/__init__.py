"""
Lutron RadioRA 2 module for interacting with the Main Repeater. Basic operations
for enumerating and controlling the loads are supported.

"""

__author__ = "Dima Zavin"
__copyright__ = "Copyright 2016, Dima Zavin"

from enum import Enum
import logging
import telnetlib
import threading
import time

from typing import Any, Callable, Dict, Type

_LOGGER = logging.getLogger(__name__)

class LutronException(Exception):
  """Top level module exception."""
  pass


class IntegrationIdExistsError(LutronException):
  """Asserted when there's an attempt to register a duplicate integration id."""
  pass


class ConnectionExistsError(LutronException):
  """Raised when a connection already exists (e.g. user calls connect() twice)."""
  pass


class InvalidSubscription(LutronException):
  """Raised when an invalid subscription is requested (e.g. calling
  Lutron.subscribe on an incompatible object."""
  pass


class LutronConnection(threading.Thread):
  """Encapsulates the connection to the Lutron controller."""
  USER_PROMPT = b'login: '
  PW_PROMPT = b'password: '
  PROMPT = b'GNET> '

  def __init__(self, host, user, password, recv_callback):
    """Initializes the lutron connection, doesn't actually connect."""
    threading.Thread.__init__(self)

    self._host = host
    self._user = user.encode('ascii')
    self._password = password.encode('ascii')
    self._telnet = None
    self._connected = False
    self._lock = threading.Lock()
    self._connect_cond = threading.Condition(lock=self._lock)
    self._recv_cb = recv_callback
    self._done = False

    self.setDaemon(True)

  def connect(self):
    """Connects to the lutron controller."""
    if self._connected or self.is_alive():
      raise ConnectionExistsError("Already connected")
    # After starting the thread we wait for it to post us
    # an event signifying that connection is established. This
    # ensures that the caller only resumes when we are fully connected.
    self.start()
    with self._lock:
      self._connect_cond.wait_for(lambda: self._connected)

  def _send_locked(self, cmd):
    """Sends the specified command to the lutron controller.

    Assumes self._lock is held.
    """
    _LOGGER.debug("Sending: %s" % cmd)
    try:
      self._telnet.write(cmd.encode('ascii') + b'\r\n')
    except BrokenPipeError:
      self._disconnect_locked()

  def send(self, cmd):
    """Sends the specified command to the lutron controller.

    Must not hold self._lock.
    """
    with self._lock:
      self._send_locked(cmd)

  def _do_login_locked(self):
    """Executes the login procedure (telnet) as well as setting up some
    connection defaults like turning off the prompt, etc."""
    self._telnet = telnetlib.Telnet(self._host)
    self._telnet.read_until(LutronConnection.USER_PROMPT)
    self._telnet.write(self._user + b'\r\n')
    self._telnet.read_until(LutronConnection.PW_PROMPT)
    self._telnet.write(self._password + b'\r\n')
    self._telnet.read_until(LutronConnection.PROMPT)

    self._send_locked("#MONITORING,12,2")
    self._send_locked("#MONITORING,255,2")
    self._send_locked("#MONITORING,3,1")
    self._send_locked("#MONITORING,4,1")
    self._send_locked("#MONITORING,5,1")
    self._send_locked("#MONITORING,6,1")
    self._send_locked("#MONITORING,8,1")

  def _disconnect_locked(self):
    """Closes the current connection. Assume self._lock is held."""
    self._connected = False
    self._connect_cond.notify_all()
    self._telnet = None
    _LOGGER.warning("Disconnected")

  def _maybe_reconnect(self):
    """Reconnects to the controller if we have been previously disconnected."""
    with self._lock:
      if not self._connected:
        _LOGGER.info("Connecting")
        self._do_login_locked()
        self._connected = True
        self._connect_cond.notify_all()
        _LOGGER.info("Connected")

  def run(self):
    """Main thread function to maintain connection and receive remote status."""
    _LOGGER.info("Started")
    while True:
      self._maybe_reconnect()
      line = ''
      try:
        # If someone is sending a command, we can lose our connection so grab a
        # copy beforehand. We don't need the lock because if the connection is
        # open, we are the only ones that will read from telnet (the reconnect
        # code runs synchronously in this loop).
        t = self._telnet
        if t is not None:
          line = t.read_until(b"\n")
      except EOFError:
        try:
          self._lock.acquire()
          self._disconnect_locked()
          continue
        finally:
          self._lock.release()
      self._recv_cb(line.decode('ascii').rstrip())


class LutronXmlDbParser(object):
  """The parser for Lutron XML database.

  The database describes all the rooms (Area), keypads (Device), and switches
  (Output). We handle the most relevant features, but some things like LEDs,
  etc. are not implemented."""

  def __init__(self, lutron, xml_db_str):
    """Initializes the XML parser, takes the raw XML data as string input."""
    self._lutron = lutron
    self._xml_db_str = xml_db_str
    self.areas = []
    self.project_name = None

  def parse(self):
    """Main entrypoint into the parser. It interprets and creates all the
    relevant Lutron objects and stuffs them into the appropriate hierarchy."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(self._xml_db_str)
    # The structure is something like this:
    # <Areas>
    #   <Area ...>
    #     <DeviceGroups ...>
    #     <Scenes ...>
    #     <ShadeGroups ...>
    #     <Outputs ...>
    #     <Areas ...>
    #       <Area ...>

    # First area is useless, it's the top-level project area that defines the
    # "house". It contains the real nested Areas tree, which is the one we want.
    top_area = root.find('Areas').find('Area')
    self.project_name = top_area.get('Name')
    areas = top_area.find('Areas')
    for area_xml in areas.getiterator('Area'):
      area = self._parse_area(area_xml)
      self.areas.append(area)
    return True

  def _parse_area(self, area_xml):
    """Parses an Area tag, which is effectively a room, depending on how the
    Lutron controller programming was done."""
    area = Area(self._lutron,
                name=area_xml.get('Name'),
                integration_id=int(area_xml.get('IntegrationID')),
                occupancy_group_id=area_xml.get('OccupancyGroupAssignedToID'))
    for output_xml in area_xml.find('Outputs'):
      output = self._parse_output(output_xml)
      area.add_output(output)
    # device group in our case means keypad
    # device_group.get('Name') is the location of the keypad
    for device_group in area_xml.find('DeviceGroups'):
      if device_group.tag == 'DeviceGroup':
        devs = device_group.find('Devices')
      elif device_group.tag == 'Device':
        devs = [device_group]
      else:
        _LOGGER.info("Unknown tag in DeviceGroups child %s" % devs)
        devs = []
      for device_xml in devs:
        if device_xml.tag != 'Device':
          continue
        if device_xml.get('DeviceType') in (
            'SEETOUCH_KEYPAD',
            'SEETOUCH_TABLETOP_KEYPAD',
            'PICO_KEYPAD',
            'HYBRID_SEETOUCH_KEYPAD',
            'MAIN_REPEATER',
            'HOMEOWNER_KEYPAD'):
          keypad = self._parse_keypad(device_xml)
          area.add_keypad(keypad)
        elif device_xml.get('DeviceType') == 'MOTION_SENSOR':
          motion_sensor = self._parse_motion_sensor(device_xml)
          area.add_sensor(motion_sensor)
        #elif device_xml.get('DeviceType') == 'VISOR_CONTROL_RECEIVER':
    return area

  def _parse_output(self, output_xml):
    """Parses an output, which is generally a switch controlling a set of
    lights/outlets, etc."""
    output = Output(self._lutron,
                    name=output_xml.get('Name'),
                    watts=int(output_xml.get('Wattage')),
                    output_type=output_xml.get('OutputType'),
                    integration_id=int(output_xml.get('IntegrationID')))
    return output

  def _parse_keypad(self, keypad_xml):
    """Parses a keypad device (the Visor receiver is technically a keypad too)."""
    keypad = Keypad(self._lutron,
                    name=keypad_xml.get('Name'),
                    integration_id=int(keypad_xml.get('IntegrationID')))
    components = keypad_xml.find('Components')
    if not components:
      return keypad
    for comp in components:
      if comp.tag != 'Component':
        continue
      comp_type = comp.get('ComponentType')
      if comp_type == 'BUTTON':
        button = self._parse_button(keypad, comp)
        keypad.add_button(button)
      elif comp_type == 'LED':
        led = self._parse_led(keypad, comp)
        keypad.add_led(led)
    return keypad

  def _parse_button(self, keypad, component_xml):
    """Parses a button device that part of a keypad."""
    button_xml = component_xml.find('Button')
    name = button_xml.get('Engraving')
    button_type = button_xml.get('ButtonType')
    direction = button_xml.get('Direction')
    # Hybrid keypads have dimmer buttons which have no engravings.
    if button_type == 'SingleSceneRaiseLower':
      name = 'Dimmer ' + direction
    if not name:
      name = "Unknown Button"
    button = Button(self._lutron, keypad,
                    name=name,
                    num=int(component_xml.get('ComponentNumber')),
                    button_type=button_type,
                    direction=direction)
    return button

  def _parse_led(self, keypad, component_xml):
    """Parses an LED device that part of a keypad."""
    component_num = int(component_xml.get('ComponentNumber'))
    led_num = component_num - 80
    led = Led(self._lutron, keypad,
              name=('LED %d' % led_num),
              led_num=led_num,
              component_num=component_num)
    return led

  def _parse_motion_sensor(self, sensor_xml):
    """Parses a motion sensor object.

    TODO: We don't actually do anything with these yet. There's a lot of info
    that needs to be managed to do this right. We'd have to manage the occupancy
    groups, what's assigned to them, and when they go (un)occupied. We'll handle
    this later.
    """
    return MotionSensor(self._lutron,
                        name=sensor_xml.get('Name'),
                        integration_id=int(sensor_xml.get('IntegrationID')))


class Lutron(object):
  """Main Lutron Controller class.

  This object owns the connection to the controller, the rooms that exist in the
  network, handles dispatch of incoming status updates, etc.
  """

  # All Lutron commands start with one of these characters
  # See http://www.lutron.com/TechnicalDocumentLibrary/040249.pdf
  OP_EXECUTE = '#'
  OP_QUERY = '?'
  OP_RESPONSE = '~'

  def __init__(self, host, user, password):
    """Initializes the Lutron object. No connection is made to the remote
    device."""
    self._host = host
    self._user = user
    self._password = password
    self._name = None
    self._conn = LutronConnection(host, user, password, self._recv)
    self._ids = {}
    self._legacy_subscribers = {}
    self._areas = []

  @property
  def areas(self):
    """Return the areas that were discovered for this Lutron controller."""
    return self._areas

  def subscribe(self, obj, handler):
    """Subscribes to status updates of the requested object.

    DEPRECATED

    The handler will be invoked when the controller sends a notification
    regarding changed state. The user can then further query the object for the
    state itself."""
    if not isinstance(obj, LutronEntity):
      raise InvalidSubscription("Subscription target not a LutronEntity")
    _LOGGER.warning("DEPRECATED: Subscribing via Lutron.subscribe is obsolete. "
                    "Please use LutronEntity.subscribe")
    if obj not in self._legacy_subscribers:
      self._legacy_subscribers[obj] = handler
      obj.subscribe(self._dispatch_legacy_subscriber, None)

  def register_id(self, cmd_type, obj):
    """Registers an object (through its integration id) to receive update
    notifications. This is the core mechanism how Output and Keypad objects get
    notified when the controller sends status updates."""
    ids = self._ids.setdefault(cmd_type, {})
    if obj.id in ids:
      raise IntegrationIdExistsError
    self._ids[cmd_type][obj.id] = obj

  def _dispatch_legacy_subscriber(self, obj, *args, **kwargs):
    """This dispatches the registered callback for 'obj'. This is only used
    for legacy subscribers since new users should register with the target
    object directly."""
    if obj in self._legacy_subscribers:
      self._legacy_subscribers[obj](obj)

  def _recv(self, line):
    """Invoked by the connection manager to process incoming data."""
    if line == '':
      return
    # Only handle query response messages, which are also sent on remote status
    # updates (e.g. user manually pressed a keypad button)
    if line[0] != Lutron.OP_RESPONSE:
      _LOGGER.debug("ignoring %s" % line)
      return
    parts = line[1:].split(',')
    cmd_type = parts[0]
    integration_id = int(parts[1])
    args = parts[2:]
    if cmd_type not in self._ids:
      _LOGGER.info("Unknown cmd %s (%s)" % (cmd_type, line))
      return
    ids = self._ids[cmd_type]
    if integration_id not in ids:
      _LOGGER.warning("Unknown id %d (%s)" % (integration_id, line))
      return
    obj = ids[integration_id]
    handled = obj.handle_update(args)

  def connect(self):
    """Connects to the Lutron controller to send and receive commands and status"""
    self._conn.connect()

  def send(self, op, cmd, integration_id, *args):
    """Formats and sends the requested command to the Lutron controller."""
    out_cmd = ",".join(
        (cmd, str(integration_id)) + tuple((str(x) for x in args)))
    self._conn.send(op + out_cmd)

  def load_xml_db(self):
    """Load the Lutron database from the server."""

    import urllib.request
    xmlfile = urllib.request.urlopen('http://' + self._host + '/DbXmlInfo.xml')
    xml_db = xmlfile.read()
    xmlfile.close()
    _LOGGER.info("Loaded xml db")

    parser = LutronXmlDbParser(lutron=self, xml_db_str=xml_db)
    assert(parser.parse())     # throw our own exception
    self._areas = parser.areas
    self._name = parser.project_name

    _LOGGER.info('Found Lutron project: %s, %d areas' % (
        self._name, len(self.areas)))

    return True


class _RequestHelper(object):
  """A class to help with sending queries to the controller and waiting for
  responses.

  It is a wrapper used to help with executing a user action
  and then waiting for an event when that action completes.

  The user calls request() and gets back a threading.Event on which they then
  wait.

  If multiple clients of a lutron object (say an Output) want to get a status
  update on the current brightness (output level), we don't want to spam the
  controller with (near)identical requests. So, if a request is pending, we
  just enqueue another waiter on the pending request and return a new Event
  object. All waiters will be woken up when the reply is received and the
  wait list is cleared.

  NOTE: Only the first enqueued action is executed as the assumption is that the
  queries will be identical in nature.
  """

  def __init__(self):
    """Initialize the request helper class."""
    self.__lock = threading.Lock()
    self.__events = []

  def request(self, action):
    """Request an action to be performed, in case one."""
    ev = threading.Event()
    first = False
    with self.__lock:
      if len(self.__events) == 0:
        first = True
      self.__events.append(ev)
    if first:
      action()
    return ev

  def notify(self):
    with self.__lock:
      events = self.__events
      self.__events = []
    for ev in events:
      ev.set()

# This describes the type signature of the callback that LutronEntity
# subscribers must provide.
LutronEventHandler = Callable[['LutronEntity', Any, 'LutronEvent', Dict], None]


class LutronEvent(Enum):
  """Base class for the events LutronEntity-derived objects can produce."""
  pass


class LutronEntity(object):
  """Base class for all the Lutron objects we'd like to manage. Just holds basic
  common info we'd rather not manage repeatedly."""

  def __init__(self, lutron, name):
    """Initializes the base class with common, basic data."""
    self._lutron = lutron
    self._name = name
    self._subscribers = []

  @property
  def name(self):
    """Returns the entity name (e.g. Pendant)."""
    return self._name

  def _dispatch_event(self, event: LutronEvent, params: Dict):
    """Dispatches the specified event to all the subscribers."""
    for handler, context in self._subscribers:
      handler(self, context, event, params)

  def subscribe(self, handler: LutronEventHandler, context):
    """Subscribes to events from this entity.

    handler: A callable object that takes the following arguments (in order)
             obj: the LutrongEntity object that generated the event
             context: user-supplied (to subscribe()) context object
             event: the LutronEvent that was generated.
             params: a dict of event-specific parameters

    context: User-supplied, opaque object that will be passed to handler.
    """
    self._subscribers.append((handler, context))

  def handle_update(self, args):
    """The handle_update callback is invoked when an event is received
    for the this entity.

    Returns:
      True - If event was valid and was handled.
      False - otherwise.
    """
    return False


class Output(LutronEntity):
  """This is the output entity in Lutron universe. This generally refers to a
  switched/dimmed load, e.g. light fixture, outlet, etc."""
  _CMD_TYPE = 'OUTPUT'
  _ACTION_ZONE_LEVEL = 1

  class Event(LutronEvent):
    """Output events that can be generated.

    LEVEL_CHANGED: The output level has changed.
        Params:
          level: new output level (float)
    """
    LEVEL_CHANGED = 1

  def __init__(self, lutron, name, watts, output_type, integration_id):
    """Initializes the Output."""
    super(Output, self).__init__(lutron, name)
    self._watts = watts
    self._output_type = output_type
    self._level = 0.0
    self._query_waiters = _RequestHelper()
    self._integration_id = integration_id

    self._lutron.register_id(Output._CMD_TYPE, self)

  def __str__(self):
    """Returns a pretty-printed string for this object."""
    return 'Output name: "%s" watts: %d type: "%s" id: %d' % (
        self._name, self._watts, self._output_type, self._integration_id)

  def __repr__(self):
    """Returns a stringified representation of this object."""
    return str({'name': self._name, 'watts': self._watts,
                'type': self._output_type, 'id': self._integration_id})

  @property
  def id(self):
    """The integration id"""
    return self._integration_id

  def handle_update(self, args):
    """Handles an event update for this object, e.g. dimmer level change."""
    _LOGGER.debug("handle_update %d -- %s" % (self._integration_id, args))
    state = int(args[0])
    if state != Output._ACTION_ZONE_LEVEL:
      return False
    level = float(args[1])
    _LOGGER.debug("Updating %d(%s): s=%d l=%f" % (
        self._integration_id, self._name, state, level))
    self._level = level
    self._query_waiters.notify()
    self._dispatch_event(Output.Event.LEVEL_CHANGED, {'level': self._level})
    return True

  def __do_query_level(self):
    """Helper to perform the actual query the current dimmer level of the
    output. For pure on/off loads the result is either 0.0 or 100.0."""
    self._lutron.send(Lutron.OP_QUERY, Output._CMD_TYPE, self._integration_id,
            Output._ACTION_ZONE_LEVEL)

  def last_level(self):
    """Returns last cached value of the output level, no query is performed."""
    return self._level

  @property
  def level(self):
    """Returns the current output level by querying the remote controller."""
    ev = self._query_waiters.request(self.__do_query_level)
    ev.wait(1.0)
    return self._level

  @level.setter
  def level(self, new_level):
    """Sets the new output level."""
    if self._level == new_level:
      return
    self._lutron.send(Lutron.OP_EXECUTE, Output._CMD_TYPE, self._integration_id,
        Output._ACTION_ZONE_LEVEL, "%.2f" % new_level)
    self._level = new_level

## At some later date, we may want to also specify fade and delay times
#  def set_level(self, new_level, fade_time, delay):
#    self._lutron.send(Lutron.OP_EXECUTE, Output._CMD_TYPE,
#        Output._ACTION_ZONE_LEVEL, new_level, fade_time, delay)

  @property
  def watts(self):
    """Returns the configured maximum wattage for this output (not an actual
    measurement)."""
    return self._watts

  @property
  def type(self):
    """Returns the output type. At present AUTO_DETECT or NON_DIM."""
    return self._output_type

  @property
  def is_dimmable(self):
    """Returns a boolean of whether or not the output is dimmable."""
    return self.type != 'NON_DIM' and not self.type.startswith('CCO_')


class KeypadComponent(LutronEntity):
  """Base class for a keypad component such as a button, or an LED."""

  def __init__(self, lutron, keypad, name, num, component_num):
    """Initializes the base keypad component class."""
    super(KeypadComponent, self).__init__(lutron, name)
    self._keypad = keypad
    self._num = num
    self._component_num = component_num

  @property
  def number(self):
    """Returns the user-friendly number of this component (e.g. Button 1,
    or LED 1."""
    return self._num

  @property
  def component_number(self):
    """Return the lutron component number, which is referenced in commands and
    events. This is different from KeypadComponent.number because this property
    is only used for interfacing with the controller."""
    return self._component_num

  def handle_update(self, action, params):
    """Handle the specified action on this component."""
    _LOGGER.debug('Keypad: "%s" Handling "%s" Action: %s Params: %s"' % (
                  self._keypad.name, self.name, action, params))
    return False


class Button(KeypadComponent):
  """This object represents a keypad button that we can trigger and handle
  events for (button presses)."""
  _ACTION_PRESS = 3
  _ACTION_RELEASE = 4

  class Event(LutronEvent):
    """Button events that can be generated.

    PRESSED: The button has been pressed.
        Params: None

    RELEASED: The button has been released. Not all buttons
              generate this event.
        Params: None
    """
    PRESSED = 1
    RELEASED = 2

  def __init__(self, lutron, keypad, name, num, button_type, direction):
    """Initializes the Button class."""
    super(Button, self).__init__(lutron, keypad, name, num, num)
    self._button_type = button_type
    self._direction = direction

  def __str__(self):
    """Pretty printed string value of the Button object."""
    return 'Button name: "%s" num: %d type: "%s" direction: "%s"' % (
        self.name, self.number, self._button_type, self._direction)

  def __repr__(self):
    """String representation of the Button object."""
    return str({'name': self.name, 'num': self.number,
               'type': self._button_type, 'direction': self._direction})

  @property
  def button_type(self):
    """Returns the button type (Toggle, MasterRaiseLower, etc.)."""
    return self._button_type

  def press(self):
    """Triggers a simulated button press to the Keypad."""
    self._lutron.send(Lutron.OP_EXECUTE, Keypad._CMD_TYPE, self._keypad.id,
                      self.component_number, Button._ACTION_PRESS)

  def handle_update(self, action, params):
    """Handle the specified action on this component."""
    _LOGGER.debug('Keypad: "%s" %s Action: %s Params: %s"' % (
                  self._keypad.name, self, action, params))
    ev_map = {
        Button._ACTION_PRESS: Button.Event.PRESSED,
        Button._ACTION_RELEASE: Button.Event.RELEASED
    }
    if action not in ev_map:
      _LOGGER.debug("Unknown action %d for button %d in keypad %d" % (
          action, self.number, self.keypad.name))
      return False
    self._dispatch_event(ev_map[action], {})
    return True


class Led(KeypadComponent):
  """This object represents a keypad LED that we can turn on/off and
  handle events for (led toggled by scenes)."""
  _ACTION_LED_STATE = 9

  class Event(LutronEvent):
    """Led events that can be generated.

    STATE_CHANGED: The button has been pressed.
        Params:
          state: The boolean value of the new LED state.
    """
    STATE_CHANGED = 1

  def __init__(self, lutron, keypad, name, led_num, component_num):
    """Initializes the Keypad LED class."""
    super(Led, self).__init__(lutron, keypad, name, led_num, component_num)
    self._state = False
    self._query_waiters = _RequestHelper()

  def __str__(self):
    """Pretty printed string value of the Led object."""
    return 'LED keypad: "%s" name: "%s" num: %d component_num: %d"' % (
        self._keypad.name, self.name, self.number, self.component_number)

  def __repr__(self):
    """String representation of the Led object."""
    return str({'keypad': self._keypad, 'name': self.name,
                'num': self.number, 'component_num': self.component_number})

  def __do_query_state(self):
    """Helper to perform the actual query for the current LED state."""
    self._lutron.send(Lutron.OP_QUERY, Keypad._CMD_TYPE, self._keypad.id,
            self.component_number, Led._ACTION_LED_STATE)

  @property
  def last_state(self):
    """Returns last cached value of the LED state, no query is performed."""
    return self._state

  @property
  def state(self):
    """Returns the current LED state by querying the remote controller."""
    ev = self._query_waiters.request(self.__do_query_state)
    ev.wait(1.0)
    return self._state

  @state.setter
  def state(self, new_state: bool):
    """Sets the new led state.

    new_state: bool
    """
    self._lutron.send(Lutron.OP_EXECUTE, Keypad._CMD_TYPE, self._keypad.id,
                      self.component_number, Led._ACTION_LED_STATE,
                      int(new_state))
    self._state = new_state

  def handle_update(self, action, params):
    """Handle the specified action on this component."""
    _LOGGER.debug('Keypad: "%s" %s Action: %s Params: %s"' % (
                  self._keypad.name, self, action, params))
    if action != Led._ACTION_LED_STATE:
      _LOGGER.debug("Unknown action %d for led %d in keypad %d" % (
          action, self.number, self.keypad.name))
      return False
    elif len(params) < 1:
      _LOGGER.debug("Unknown params %s (action %d on led %d in keypad %d)" % (
          params, action, self.number, self.keypad.name))
      return False
    self._state = bool(params[0])
    self._query_waiters.notify()
    self._dispatch_event(Led.Event.STATE_CHANGED, {'state': self._state})
    return True


class Keypad(LutronEntity):
  """Object representing a Lutron keypad.

  Currently we don't really do much with it except handle the events
  (and drop them on the floor).
  """
  _CMD_TYPE = 'DEVICE'

  def __init__(self, lutron, name, integration_id):
    """Initializes the Keypad object."""
    super(Keypad, self).__init__(lutron, name)
    self._buttons = []
    self._leds = []
    self._components = {}
    self._integration_id = integration_id

    self._lutron.register_id(Keypad._CMD_TYPE, self)

  def add_button(self, button):
    """Adds a button that's part of this keypad. We'll use this to
    dispatch button events."""
    self._buttons.append(button)
    self._components[button.component_number] = button

  def add_led(self, led):
    """Add an LED that's part of this keypad."""
    self._leds.append(led)
    self._components[led.component_number] = led

  @property
  def id(self):
    """The integration id"""
    return self._integration_id

  @property
  def name(self):
    """Returns the name of this keypad"""
    return self._name

  @property
  def buttons(self):
    """Return a tuple of buttons for this keypad."""
    return tuple(button for button in self._buttons)

  @property
  def leds(self):
    """Return a tuple of leds for this keypad."""
    return tuple(led for led in self._leds)

  def handle_update(self, args):
    """The callback invoked by the main event loop if there's an event from this keypad."""
    component = int(args[0])
    action = int(args[1])
    params = [int(x) for x in args[2:]]
    _LOGGER.debug("Updating %d(%s): c=%d a=%d params=%s" % (
        self._integration_id, self._name, component, action, params))
    if component in self._components:
      return self._components[component].handle_update(action, params)
    return False


class MotionSensor(object):
  """Placeholder class for the motion sensor device.

  TODO: Actually implement this.
  """
  def __init__(self, lutron, name, integration_id):
    """Initializes the motion sensor object."""
    self._lutron = lutron
    self._name = name
    self._integration_id = integration_id


class Area(object):
  """An area (i.e. a room) that contains devices/outputs/etc."""
  def __init__(self, lutron, name, integration_id, occupancy_group_id):
    self._lutron = lutron
    self._name = name
    self._integration_id = integration_id
    self._occupancy_group_id = occupancy_group_id
    self._outputs = []
    self._keypads = []
    self._sensors = []

  def add_output(self, output):
    """Adds an output object that's part of this area, only used during
    initial parsing."""
    self._outputs.append(output)

  def add_keypad(self, keypad):
    """Adds a keypad object that's part of this area, only used during
    initial parsing."""
    self._keypads.append(keypad)

  def add_sensor(self, sensor):
    """Adds a motion sensor object that's part of this area, only used during
    initial parsing."""
    self._sensors.append(sensor)

  @property
  def name(self):
    """Returns the name of this area."""
    return self._name

  @property
  def id(self):
    """The integration id of the area."""
    return self._integration_id

  @property
  def outputs(self):
    """Return the tuple of the Outputs from this area."""
    return tuple(output for output in self._outputs)

  @property
  def keypads(self):
    """Return the tuple of the Keypads from this area."""
    return tuple(keypad for keypad in self._keypads)

  @property
  def sensors(self):
    """Return the tuple of the MotionSensors from this area."""
    return tuple(sensor for sensor in self._sensors)
