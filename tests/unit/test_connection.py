# Copyright 2013-2014 DataStax, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
try:
    import unittest2 as unittest
except ImportError:
    import unittest  # noqa

from mock import Mock, ANY, call
import six
from six import BytesIO
import time
from threading import Lock

from cassandra.cluster import Cluster
from cassandra.connection import (Connection, HEADER_DIRECTION_TO_CLIENT,
                                  HEADER_DIRECTION_FROM_CLIENT, ProtocolError,
                                  locally_supported_compressions, ConnectionHeartbeat)
from cassandra.marshal import uint8_pack, uint32_pack
from cassandra.protocol import (write_stringmultimap, write_int, write_string,
                                SupportedMessage)


class ConnectionTest(unittest.TestCase):

    protocol_version = 2

    def make_connection(self):
        c = Connection('1.2.3.4')
        c._socket = Mock()
        c._socket.send.side_effect = lambda x: len(x)
        return c

    def make_header_prefix(self, message_class, version=2, stream_id=0):
        return six.binary_type().join(map(uint8_pack, [
            0xff & (HEADER_DIRECTION_TO_CLIENT | version),
            0,  # flags (compression)
            stream_id,
            message_class.opcode  # opcode
        ]))

    def make_options_body(self):
        options_buf = BytesIO()
        write_stringmultimap(options_buf, {
            'CQL_VERSION': ['3.0.1'],
            'COMPRESSION': []
        })
        return options_buf.getvalue()

    def make_error_body(self, code, msg):
        buf = BytesIO()
        write_int(buf, code)
        write_string(buf, msg)
        return buf.getvalue()

    def make_msg(self, header, body=""):
        return header + uint32_pack(len(body)) + body

    def test_bad_protocol_version(self, *args):
        c = self.make_connection()
        c._callbacks = Mock()
        c.defunct = Mock()

        # read in a SupportedMessage response
        header = self.make_header_prefix(SupportedMessage, version=0x04)
        options = self.make_options_body()
        message = self.make_msg(header, options)
        c.process_msg(message, len(message) - 8)

        # make sure it errored correctly
        c.defunct.assert_called_once_with(ANY)
        args, kwargs = c.defunct.call_args
        self.assertIsInstance(args[0], ProtocolError)

    def test_bad_header_direction(self, *args):
        c = self.make_connection()
        c._callbacks = Mock()
        c.defunct = Mock()

        # read in a SupportedMessage response
        header = six.binary_type().join(uint8_pack(i) for i in (
            0xff & (HEADER_DIRECTION_FROM_CLIENT | self.protocol_version),
            0,  # flags (compression)
            0,
            SupportedMessage.opcode  # opcode
        ))
        options = self.make_options_body()
        message = self.make_msg(header, options)
        c.process_msg(message, len(message) - 8)

        # make sure it errored correctly
        c.defunct.assert_called_once_with(ANY)
        args, kwargs = c.defunct.call_args
        self.assertIsInstance(args[0], ProtocolError)

    def test_negative_body_length(self, *args):
        c = self.make_connection()
        c._callbacks = Mock()
        c.defunct = Mock()

        # read in a SupportedMessage response
        header = self.make_header_prefix(SupportedMessage)
        options = self.make_options_body()
        message = self.make_msg(header, options)
        c.process_msg(message, -13)

        # make sure it errored correctly
        c.defunct.assert_called_once_with(ANY)
        args, kwargs = c.defunct.call_args
        self.assertIsInstance(args[0], ProtocolError)

    def test_unsupported_cql_version(self, *args):
        c = self.make_connection()
        c._callbacks = {0: c._handle_options_response}
        c.defunct = Mock()
        c.cql_version = "3.0.3"

        # read in a SupportedMessage response
        header = self.make_header_prefix(SupportedMessage)

        options_buf = BytesIO()
        write_stringmultimap(options_buf, {
            'CQL_VERSION': ['7.8.9'],
            'COMPRESSION': []
        })
        options = options_buf.getvalue()

        message = self.make_msg(header, options)
        c.process_msg(message, len(message) - 8)

        # make sure it errored correctly
        c.defunct.assert_called_once_with(ANY)
        args, kwargs = c.defunct.call_args
        self.assertIsInstance(args[0], ProtocolError)

    def test_prefer_lz4_compression(self, *args):
        c = self.make_connection()
        c._callbacks = {0: c._handle_options_response}
        c.defunct = Mock()
        c.cql_version = "3.0.3"

        locally_supported_compressions.pop('lz4', None)
        locally_supported_compressions.pop('snappy', None)
        locally_supported_compressions['lz4'] = ('lz4compress', 'lz4decompress')
        locally_supported_compressions['snappy'] = ('snappycompress', 'snappydecompress')

        # read in a SupportedMessage response
        header = self.make_header_prefix(SupportedMessage)

        options_buf = BytesIO()
        write_stringmultimap(options_buf, {
            'CQL_VERSION': ['3.0.3'],
            'COMPRESSION': ['snappy', 'lz4']
        })
        options = options_buf.getvalue()

        message = self.make_msg(header, options)
        c.process_msg(message, len(message) - 8)

        self.assertEqual(c.decompressor, locally_supported_compressions['lz4'][1])

    def test_requested_compression_not_available(self, *args):
        c = self.make_connection()
        c._callbacks = {0: c._handle_options_response}
        c.defunct = Mock()
        # request lz4 compression
        c.compression = "lz4"

        locally_supported_compressions.pop('lz4', None)
        locally_supported_compressions.pop('snappy', None)
        locally_supported_compressions['lz4'] = ('lz4compress', 'lz4decompress')
        locally_supported_compressions['snappy'] = ('snappycompress', 'snappydecompress')

        # read in a SupportedMessage response
        header = self.make_header_prefix(SupportedMessage)

        # the server only supports snappy
        options_buf = BytesIO()
        write_stringmultimap(options_buf, {
            'CQL_VERSION': ['3.0.3'],
            'COMPRESSION': ['snappy']
        })
        options = options_buf.getvalue()

        message = self.make_msg(header, options)
        c.process_msg(message, len(message) - 8)

        # make sure it errored correctly
        c.defunct.assert_called_once_with(ANY)
        args, kwargs = c.defunct.call_args
        self.assertIsInstance(args[0], ProtocolError)

    def test_use_requested_compression(self, *args):
        c = self.make_connection()
        c._callbacks = {0: c._handle_options_response}
        c.defunct = Mock()
        # request snappy compression
        c.compression = "snappy"

        locally_supported_compressions.pop('lz4', None)
        locally_supported_compressions.pop('snappy', None)
        locally_supported_compressions['lz4'] = ('lz4compress', 'lz4decompress')
        locally_supported_compressions['snappy'] = ('snappycompress', 'snappydecompress')

        # read in a SupportedMessage response
        header = self.make_header_prefix(SupportedMessage)

        # the server only supports snappy
        options_buf = BytesIO()
        write_stringmultimap(options_buf, {
            'CQL_VERSION': ['3.0.3'],
            'COMPRESSION': ['snappy', 'lz4']
        })
        options = options_buf.getvalue()

        message = self.make_msg(header, options)
        c.process_msg(message, len(message) - 8)

        self.assertEqual(c.decompressor, locally_supported_compressions['snappy'][1])

    def test_disable_compression(self, *args):
        c = self.make_connection()
        c._callbacks = {0: c._handle_options_response}
        c.defunct = Mock()
        # disable compression
        c.compression = False

        locally_supported_compressions.pop('lz4', None)
        locally_supported_compressions.pop('snappy', None)
        locally_supported_compressions['lz4'] = ('lz4compress', 'lz4decompress')
        locally_supported_compressions['snappy'] = ('snappycompress', 'snappydecompress')

        # read in a SupportedMessage response
        header = self.make_header_prefix(SupportedMessage)

        # the server only supports snappy
        options_buf = BytesIO()
        write_stringmultimap(options_buf, {
            'CQL_VERSION': ['3.0.3'],
            'COMPRESSION': ['snappy', 'lz4']
        })
        options = options_buf.getvalue()

        message = self.make_msg(header, options)
        c.process_msg(message, len(message) - 8)

        self.assertEqual(c.decompressor, None)

    def test_not_implemented(self):
        """
        Ensure the following methods throw NIE's. If not, come back and test them.
        """
        c = self.make_connection()

        self.assertRaises(NotImplementedError, c.close)
        self.assertRaises(NotImplementedError, c.register_watcher, None, None)
        self.assertRaises(NotImplementedError, c.register_watchers, None)

    def test_set_keyspace_blocking(self):
        c = self.make_connection()

        self.assertEqual(c.keyspace, None)
        c.set_keyspace_blocking(None)
        self.assertEqual(c.keyspace, None)

        c.keyspace = 'ks'
        c.set_keyspace_blocking('ks')
        self.assertEqual(c.keyspace, 'ks')

    def test_set_connection_class(self):
        cluster = Cluster(connection_class='test')
        self.assertEqual('test', cluster.connection_class)


class ConnectionHeartbeatTest(unittest.TestCase):

    @staticmethod
    def make_get_holders(len):
        holders = []
        for _ in range(len):
            holder = Mock()
            holder.get_connections = Mock(return_value=[])
            holders.append(holder)
        get_holders = Mock(return_value=holders)
        return get_holders

    def run_heartbeat(self, get_holders_fun, count=2, interval=0.05):
        ch = ConnectionHeartbeat(interval, get_holders_fun)
        time.sleep(interval * count)
        ch.stop()
        ch.join()
        self.assertTrue(get_holders_fun.call_count)

    def test_empty_connections(self):
        count = 3
        get_holders = self.make_get_holders(1)

        self.run_heartbeat(get_holders, count)

        self.assertGreaterEqual(get_holders.call_count, count - 1)  # lower bound to account for thread spinup time
        self.assertLessEqual(get_holders.call_count, count)
        holder = get_holders.return_value[0]
        holder.get_connections.assert_has_calls([call()] * get_holders.call_count)

    def test_idle_non_idle(self):
        request_id = 999

        # connection.send_msg(OptionsMessage(), connection.get_request_id(), self._options_callback)
        def send_msg(msg, req_id, msg_callback):
            msg_callback(SupportedMessage([], {}))

        idle_connection = Mock(spec=Connection, host='localhost',
                               max_request_id=127,
                               lock=Lock(),
                               in_flight=0, is_idle=True,
                               is_defunct=False, is_closed=False,
                               get_request_id=lambda: request_id,
                               send_msg=Mock(side_effect=send_msg))
        non_idle_connection = Mock(spec=Connection, in_flight=0, is_idle=False, is_defunct=False, is_closed=False)

        get_holders = self.make_get_holders(1)
        holder = get_holders.return_value[0]
        holder.get_connections.return_value.append(idle_connection)
        holder.get_connections.return_value.append(non_idle_connection)

        self.run_heartbeat(get_holders)

        holder.get_connections.assert_has_calls([call()] * get_holders.call_count)
        self.assertEqual(idle_connection.in_flight, 0)
        self.assertEqual(non_idle_connection.in_flight, 0)

        idle_connection.send_msg.assert_has_calls([call(ANY, request_id, ANY)] * get_holders.call_count)
        self.assertEqual(non_idle_connection.send_msg.call_count, 0)

    def test_closed_defunct(self):
        get_holders = self.make_get_holders(1)
        closed_connection = Mock(spec=Connection, in_flight=0, is_idle=False, is_defunct=False, is_closed=True)
        defunct_connection = Mock(spec=Connection, in_flight=0, is_idle=False, is_defunct=True, is_closed=False)
        holder = get_holders.return_value[0]
        holder.get_connections.return_value.append(closed_connection)
        holder.get_connections.return_value.append(defunct_connection)

        self.run_heartbeat(get_holders)

        holder.get_connections.assert_has_calls([call()] * get_holders.call_count)
        self.assertEqual(closed_connection.in_flight, 0)
        self.assertEqual(defunct_connection.in_flight, 0)
        self.assertEqual(closed_connection.send_msg.call_count, 0)
        self.assertEqual(defunct_connection.send_msg.call_count, 0)

    def test_no_req_ids(self):
        in_flight = 3

        get_holders = self.make_get_holders(1)
        max_connection = Mock(spec=Connection, host='localhost',
                              max_request_id=in_flight, in_flight=in_flight,
                              is_idle=True, is_defunct=False, is_closed=False)
        holder = get_holders.return_value[0]
        holder.get_connections.return_value.append(max_connection)

        self.run_heartbeat(get_holders)

        holder.get_connections.assert_has_calls([call()] * get_holders.call_count)
        self.assertEqual(max_connection.in_flight, in_flight)
        self.assertEqual(max_connection.send_msg.call_count, 0)
        self.assertEqual(max_connection.send_msg.call_count, 0)
        max_connection.defunct.assert_has_calls([call(ANY)] * get_holders.call_count)
        holder.return_connection.assert_has_calls([call(max_connection)] * get_holders.call_count)

    def test_unexpected_response(self):
        request_id = 999

        get_holders = self.make_get_holders(1)

        def send_msg(msg, req_id, msg_callback):
            msg_callback(object())

        connection = Mock(spec=Connection, host='localhost',
                          max_request_id=127,
                          lock=Lock(),
                          in_flight=0, is_idle=True,
                          is_defunct=False, is_closed=False,
                          get_request_id=lambda: request_id,
                          send_msg=Mock(side_effect=send_msg))
        holder = get_holders.return_value[0]
        holder.get_connections.return_value.append(connection)

        self.run_heartbeat(get_holders)

        self.assertEqual(connection.in_flight, get_holders.call_count)
        connection.send_msg.assert_has_calls([call(ANY, request_id, ANY)] * get_holders.call_count)
        connection.defunct.assert_has_calls([call(ANY)] * get_holders.call_count)
        exc = connection.defunct.call_args_list[0][0][0]
        self.assertIsInstance(exc, Exception)
        self.assertEqual(exc.args, Exception('Connection heartbeat failure').args)
        holder.return_connection.assert_has_calls([call(connection)] * get_holders.call_count)

    def test_timeout(self):
        request_id = 999

        get_holders = self.make_get_holders(1)

        def send_msg(msg, req_id, msg_callback):
            pass

        connection = Mock(spec=Connection, host='localhost',
                          max_request_id=127,
                          lock=Lock(),
                          in_flight=0, is_idle=True,
                          is_defunct=False, is_closed=False,
                          get_request_id=lambda: request_id,
                          send_msg=Mock(side_effect=send_msg))
        holder = get_holders.return_value[0]
        holder.get_connections.return_value.append(connection)

        self.run_heartbeat(get_holders)

        self.assertEqual(connection.in_flight, get_holders.call_count)
        connection.send_msg.assert_has_calls([call(ANY, request_id, ANY)] * get_holders.call_count)
        connection.defunct.assert_has_calls([call(ANY)] * get_holders.call_count)
        exc = connection.defunct.call_args_list[0][0][0]
        self.assertIsInstance(exc, Exception)
        self.assertEqual(exc.args, Exception('Connection heartbeat failure').args)
        holder.return_connection.assert_has_calls([call(connection)] * get_holders.call_count)
