from .constants import PropertyAccess
from .signature import SignatureTree, SignatureBodyMismatchError, Variant
from . import introspection as intr
from .errors import SignalDisabledError

from functools import wraps
import inspect
from typing import no_type_check_decorator, Dict, List, Any

# TODO: if the user uses `from __future__ import annotations` in their code,
# the annotation inspection will not work because of PEP 563. We will get
# something that needs to be evaled because type hints will become "forward
# definitions". You can do this eval automatically with
# typing.get_type_hints(). This fails without the __future__ import on
# python 3.7 but will always succeed on python4. I don't know how to tell if
# the user has imported the future annotation feature. We might just not
# support the future import on python3 for now and do a check for python4
# later. I really hope they keep supporting this use case.


class _Method:
    def __init__(self, fn, name, disabled=False):
        in_signature = ''
        out_signature = ''

        inspection = inspect.signature(fn)

        in_args = []
        for i, param in enumerate(inspection.parameters.values()):
            if i == 0:
                # first is self
                continue
            if param.annotation is inspect.Signature.empty:
                raise ValueError(
                    'method parameters must specify the dbus type string as an annotation')
            in_args.append(intr.Arg(param.annotation, intr.ArgDirection.IN, param.name))
            in_signature += param.annotation

        out_args = []
        if inspection.return_annotation is not inspect.Signature.empty:
            out_signature = inspection.return_annotation
            for type_ in SignatureTree(inspection.return_annotation).types:
                out_args.append(intr.Arg(type_, intr.ArgDirection.OUT))

        self.name = name
        self.fn = fn
        self.disabled = disabled
        self.introspection = intr.Method(name, in_args, out_args)
        self.in_signature = in_signature
        self.out_signature = out_signature
        self.in_signature_tree = SignatureTree(in_signature)
        self.out_signature_tree = SignatureTree(out_signature)


def method(name: str = None, disabled: bool = False):
    """A decorator to mark a class method of a :class:`ServiceInterface` to be a DBus service method.

    The parameters and return value must each be annotated with a signature
    string of a single complete DBus type.

    This class method will be called when a client calls the method on the DBus
    interface. The parameters given to the function come from the calling
    client and will conform to the dbus-next type system. The parameters
    returned will be returned to the calling client and must conform to the
    dbus-next type system. If multiple parameters are returned, they must be
    contained within a :class:`list`.

    The decorated method may raise a :class:`DBusError <dbus_next.DBusError>`
    to return an error to the client.

    :param name: The member name that DBus clients will use to call this method. Defaults to the name of the class method.
    :type name: str
    :param disabled: If set to true, the method will not be visible to clients.
    :type disabled: bool

    :example:

    ::

        @method()
        def echo(self, val: 's') -> 's':
            return val

        @method()
        def echo_two(self, val1: 's', val2: 'u') -> 'su':
            return [val1, val2]
    """
    if name is not None and type(name) is not str:
        raise TypeError('name must be a string')
    if type(disabled) is not bool:
        raise TypeError('disabled must be a bool')

    @no_type_check_decorator
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            fn(*args, **kwargs)

        fn_name = name if name else fn.__name__
        wrapped.__dict__['__DBUS_METHOD'] = _Method(fn, fn_name, disabled=disabled)

        return wrapped

    return decorator


class _Signal:
    def __init__(self, fn, name, disabled=False):
        inspection = inspect.signature(fn)

        args = []
        signature = ''
        signature_tree = None

        if inspection.return_annotation is not inspect.Signature.empty:
            signature = inspection.return_annotation
            signature_tree = SignatureTree(signature)
            for type_ in signature_tree.types:
                args.append(intr.Arg(type_, intr.ArgDirection.OUT))
        else:
            signature = ''
            signature_tree = SignatureTree('')

        self.signature = signature
        self.signature_tree = signature_tree
        self.name = name
        self.disabled = disabled
        self.introspection = intr.Signal(self.name, args)


def signal(name: str = None, disabled: bool = False):
    """A decorator to mark a class method of a :class:`ServiceInterface` to be a DBus signal.

    The signal is broadcast on the bus when the decorated class method is
    called by the user.

    If the signal has an out argument, the class method must have a return type
    annotation with a signature string of a single complete DBus type and the
    return value of the class method must conform to the dbus-next type system.
    If the signal has multiple out arguments, they must be returned within a
    ``list``.

    :param name: The member name that will be used for this signal. Defaults to
        the name of the class method.
    :type name: str
    :param disabled: If set to true, the signal will not be visible to clients.
    :type disabled: bool

    :example:

    ::

        @signal()
        def string_signal(self, val) -> 's':
            return val

        @signal()
        def two_strings_signal(self, val1, val2) -> 'ss':
            return [val1, val2]
    """
    if name is not None and type(name) is not str:
        raise TypeError('name must be a string')
    if type(disabled) is not bool:
        raise TypeError('disabled must be a bool')

    @no_type_check_decorator
    def decorator(fn):
        fn_name = name if name else fn.__name__
        signal = _Signal(fn, fn_name, disabled)

        @wraps(fn)
        def wrapped(self, *args, **kwargs):
            if signal.disabled:
                raise SignalDisabledError('Tried to call a disabled signal')
            result = fn(self, *args, **kwargs)
            ServiceInterface._handle_signal(self, signal, result)
            return result

        wrapped.__dict__['__DBUS_SIGNAL'] = signal

        return wrapped

    return decorator


class _Property(property):
    def set_options(self, options):
        self.options = getattr(self, 'options', {})
        for k, v in options.items():
            self.options[k] = v

        if 'name' in options and options['name'] is not None:
            self.name = options['name']
        else:
            self.name = self.prop_getter.__name__

        if 'access' in options:
            self.access = PropertyAccess(options['access'])
        else:
            self.access = PropertyAccess.READWRITE

        if 'disabled' in options:
            self.disabled = options['disabled']
        else:
            self.disabled = False

        self.introspection = intr.Property(self.name, self.signature, self.access)

        self.__dict__['__DBUS_PROPERTY'] = True

    def __init__(self, fn, *args, **kwargs):
        self.prop_getter = fn
        self.prop_setter = None

        sig = inspect.signature(fn)
        if len(sig.parameters) != 1:
            raise ValueError('the property must only have the "self" input parameter')

        if sig.return_annotation is inspect.Signature.empty:
            raise ValueError(
                'the property must specify the dbus type string as a return annotation string')

        self.signature = sig.return_annotation
        tree = SignatureTree(sig.return_annotation)

        if len(tree.types) != 1:
            raise ValueError('the property signature must be a single complete type')

        self.type = tree.types[0]

        if 'options' in kwargs:
            options = kwargs['options']
            self.set_options(options)
            del kwargs['options']

        super().__init__(fn, *args, **kwargs)

    def setter(self, fn, **kwargs):
        # XXX The setter decorator seems to be recreating the class in the list
        # of class members and clobbering the options so we need to reset them.
        # Why does it do that?
        result = super().setter(fn, **kwargs)
        result.prop_setter = fn
        result.set_options(self.options)
        return result


def dbus_property(access: PropertyAccess = PropertyAccess.READWRITE,
                  name: str = None,
                  disabled: bool = False):
    """A decorator to mark a class method of a :class:`ServiceInterface` to be a DBus property.

    The class method must be a Python getter method with a return annotation
    that is a signature string of a single complete DBus type. When a client
    gets the property through the ``org.freedesktop.DBus.Properties``
    interface, the getter will be called and the resulting value will be
    returned to the client.

    If the property is writable, it must have a setter method that takes a
    single parameter that is annotated with the same signature. When a client
    sets the property through the ``org.freedesktop.DBus.Properties``
    interface, the setter will be called with the value from the calling
    client.

    The parameters of the getter and the setter must conform to the dbus-next
    type system. The getter or the setter may raise a :class:`DBusError
    <dbus_next.DBusError>` to return an error to the client.

    :param name: The name that DBus clients will use to interact with this
        property on the bus.
    :type name: str
    :param disabled: If set to true, the property will not be visible to
        clients.
    :type disabled: bool

    :example:

    ::

        @dbus_property()
        def string_prop(self) -> 's':
            return self._string_prop

        @string_prop.setter
        def string_prop(self, val: 's'):
            self._string_prop = val
    """
    if type(access) is not PropertyAccess:
        raise TypeError('access must be a PropertyAccess class')
    if name is not None and type(name) is not str:
        raise TypeError('name must be a string')
    if type(disabled) is not bool:
        raise TypeError('disabled must be a bool')

    @no_type_check_decorator
    def decorator(fn):
        options = {'name': name, 'access': access, 'disabled': disabled}
        return _Property(fn, options=options)

    return decorator


class ServiceInterface:
    """An abstract class that can be extended by the user to define DBus services.

    Instances of :class:`ServiceInterface` can be exported on a path of the bus
    with the :class:`export <dbus_next.message_bus.BaseMessageBus.export>`
    method of a :class:`MessageBus <dbus_next.message_bus.BaseMessageBus>`.

    Use the :func:`@method <dbus_next.service.method>`, :func:`@dbus_property
    <dbus_next.service.dbus_property>`, and :func:`@signal
    <dbus_next.service.signal>` decorators to mark class methods as DBus
    methods, properties, and signals respectively.

    :ivar name: The name of this interface as it appears to clients. Must be a
        valid interface name.
    :vartype name: str
    """

    def __init__(self, name: str):
        # TODO cannot be overridden by a dbus member
        self.name = name
        self.__methods = []
        self.__properties = []
        self.__signals = []
        self.__buses = set()

        for name, member in inspect.getmembers(type(self)):
            member_dict = getattr(member, '__dict__', {})
            if type(member) is _Property:
                # XXX The getter and the setter may show up as different
                # members if they have different names. But if they have the
                # same name, they will be the same member. So we try to merge
                # them together here. I wish we could make this cleaner.
                found = False
                for prop in self.__properties:
                    if prop.prop_getter is member.prop_getter:
                        found = True
                        if member.prop_setter is not None:
                            prop.prop_setter = member.prop_setter

                if not found:
                    self.__properties.append(member)
            elif '__DBUS_METHOD' in member_dict:
                method = member_dict['__DBUS_METHOD']
                assert type(method) is _Method
                self.__methods.append(method)
            elif '__DBUS_SIGNAL' in member_dict:
                signal = member_dict['__DBUS_SIGNAL']
                assert type(signal) is _Signal
                self.__signals.append(signal)

        # validate that writable properties have a setter
        for prop in self.__properties:
            if prop.access.writable() and prop.prop_setter is None:
                raise ValueError(f'property "{member.name}" is writable but does not have a setter')

    def emit_properties_changed(self,
                                changed_properties: Dict[str, Any],
                                invalidated_properties: List[str] = []):
        """Emit the ``org.freedesktop.DBus.Properties.PropertiesChanged`` signal.

        This signal is intended to be used to alert clients when a property of
        the interface has changed.

        :param changed_properties: The keys must be the names of properties exposed by this bus. The values must be valid for the signature of those properties.
        :type changed_properties: dict(str, Any)
        :param invalidated_properties: A list of names of properties that are now invalid (presumably for clients who cache the value).
        :type invalidated_properties: list(str)
        """
        # TODO cannot be overridden by a dbus member
        variant_dict = {}

        for prop in ServiceInterface._get_properties(self):
            if prop.name in changed_properties:
                variant_dict[prop.name] = Variant(prop.signature, changed_properties[prop.name])

        body = [self.name, variant_dict, invalidated_properties]
        for bus in ServiceInterface._get_buses(self):
            bus._interface_signal_notify(self, 'org.freedesktop.DBus.Properties',
                                         'PropertiesChanged', 'sa{sv}as', body)

    def introspect(self) -> intr.Interface:
        """Get introspection information for this interface.

        This might be useful for creating clients for the interface or examining the introspection output of an interface.

        :returns: The introspection data for the interface.
        :rtype: :class:`dbus_next.introspection.Interface`
        """
        # TODO cannot be overridden by a dbus member
        return intr.Interface(self.name,
                              methods=[
                                  method.introspection
                                  for method in ServiceInterface._get_methods(self)
                                  if not method.disabled
                              ],
                              signals=[
                                  signal.introspection
                                  for signal in ServiceInterface._get_signals(self)
                                  if not signal.disabled
                              ],
                              properties=[
                                  prop.introspection
                                  for prop in ServiceInterface._get_properties(self)
                                  if not prop.disabled
                              ])

    @staticmethod
    def _add_bus(interface, bus):
        interface.__buses.add(bus)

    @staticmethod
    def _remove_bus(interface, bus):
        interface.__buses.remove(bus)

    @staticmethod
    def _get_buses(interface):
        return interface.__buses

    @staticmethod
    def _get_properties(interface):
        return interface.__properties

    @staticmethod
    def _get_methods(interface):
        return interface.__methods

    @staticmethod
    def _get_signals(interface):
        return interface.__signals

    @staticmethod
    def _fn_result_to_body(result, signature_tree):
        out_len = len(signature_tree.types)
        if result is None:
            body = []
        elif out_len == 0:
            raise SignatureBodyMismatchError('Function was not expected to return an argument')
        elif out_len == 1:
            body = [result]
        elif type(result) is not list:
            raise SignatureBodyMismatchError('Expected function to return a list of arguments')
        else:
            body = result

        return body

    @staticmethod
    def _handle_signal(interface, signal, result):
        body = ServiceInterface._fn_result_to_body(result, signal.signature_tree)
        for bus in ServiceInterface._get_buses(interface):
            bus._interface_signal_notify(interface, interface.name, signal.name, signal.signature,
                                         body)
