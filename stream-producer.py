#! /usr/bin/env python3

# -----------------------------------------------------------------------------
# stream-producer.py Create a stream.
# - Uses a "pipes and filters" design pattern
# -----------------------------------------------------------------------------

from glob import glob
import argparse
import collections
import csv
import fastavro
import json
import confluent_kafka
import linecache
import logging
import os
import pandas
import pika
import queue
import random
import signal
import string
import threading
import multiprocessing
import sys
import time
import urllib.request
import urllib.parse

# Python 2 / 3 migration.

# try:
#     from urllib.request import urlopen
# except ImportError:
#     from urllib2 import urlopen
#
# try:
#     from urllib.parse import urlparse
# except ImportError:
#     from urlparse import urlparse

__all__ = []
__version__ = "1.0.0"  # See https://www.python.org/dev/peps/pep-0396/
__date__ = '2020-04-07'
__updated__ = '2020-06-18'

SENZING_PRODUCT_ID = "5014"  # See https://github.com/Senzing/knowledge-base/blob/master/lists/senzing-product-ids.md
log_format = '%(asctime)s %(message)s'

# Working with bytes.

KILOBYTES = 1024
MEGABYTES = 1024 * KILOBYTES
GIGABYTES = 1024 * MEGABYTES

# Random sentinel to indicate end of service

QUEUE_SENTINEL = ".{0}.".format(''.join([random.choice(string.ascii_letters + string.digits) for n in range(32)]))

# The "configuration_locator" describes where configuration variables are in:
# 1) Command line options, 2) Environment variables, 3) Configuration files, 4) Default values

configuration_locator = {
    "debug": {
        "default": False,
        "env": "SENZING_DEBUG",
        "cli": "debug"
    },
    "default_data_source": {
        "default": None,
        "env": "SENZING_DEFAULT_DATA_SOURCE",
        "cli": "default-data-source",
    },
    "default_entity_type": {
        "default": None,
        "env": "SENZING_DEFAULT_ENTITY_TYPE",
        "cli": "default-entity-type"
    },
    "delay_in_seconds": {
        "default": 0,
        "env": "SENZING_DELAY_IN_SECONDS",
        "cli": "delay-in-seconds"
    },
    "input_url": {
        "default": "https://s3.amazonaws.com/public-read-access/TestDataSets/loadtest-dataset-1M.json",
        "env": "SENZING_INPUT_URL",
        "cli": "input-url",
    },
    "kafka_bootstrap_server": {
        "default": "localhost:9092",
        "env": "SENZING_KAFKA_BOOTSTRAP_SERVER",
        "cli": "kafka-bootstrap-server",
    },
    "kafka_poll_interval": {
        "default": 100,
        "env": "SENZING_KAFKA_POLL_INTERVAL",
        "cli": "kafka-poll-interval",
    },
    "kafka_topic": {
        "default": "senzing-kafka-topic",
        "env": "SENZING_KAFKA_TOPIC",
        "cli": "kafka-topic",
    },
    "monitoring_period_in_seconds": {
#         "default": 60 * 10,
        "default": 30,
        "env": "SENZING_MONITORING_PERIOD_IN_SECONDS",
        "cli": "monitoring-period-in-seconds",
    },
    "password": {
        "default": None,
        "env": "SENZING_PASSWORD",
        "cli": "password"
    },
    "rabbitmq_exchange": {
        "default": "",
        "env": "SENZING_RABBITMQ_EXCHANGE",
        "cli": "rabbitmq-exchange",
    },
    "rabbitmq_host": {
        "default": "localhost",
        "env": "SENZING_RABBITMQ_HOST",
        "cli": "rabbitmq-host",
    },
    "rabbitmq_password": {
        "default": "bitnami",
        "env": "SENZING_RABBITMQ_PASSWORD",
        "cli": "rabbitmq-password",
    },
    "rabbitmq_port": {
        "default": "5672",
        "env": "SENZING_RABBITMQ_PORT",
        "cli": "rabbitmq-port",
    },
    "rabbitmq_queue": {
        "default": "senzing-rabbitmq-queue",
        "env": "SENZING_RABBITMQ_QUEUE",
        "cli": "rabbitmq-queue",
    },
    "rabbitmq_username": {
        "default": "user",
        "env": "SENZING_RABBITMQ_USERNAME",
        "cli": "rabbitmq-username",
    },
    "read_queue_maxsize": {
        "default": 50,
        "env": "SENZING_READ_QUEUE_MAXSIZE",
        "cli": "read-queue-maxsize"
    },
    "record_max": {
        "default": None,
        "env": "SENZING_RECORD_MAX",
        "cli": "record-max",
    },
    "record_min": {
        "default": None,
        "env": "SENZING_RECORD_MIN",
        "cli": "record-min",
    },
    "record_monitor": {
        "default": "10000",
        "env": "SENZING_RECORD_MONITOR",
        "cli": "record-monitor",
    },
    "sleep_time_in_seconds": {
        "default": 0,
        "env": "SENZING_SLEEP_TIME_IN_SECONDS",
        "cli": "sleep-time-in-seconds"
    },
    "subcommand": {
        "default": None,
        "env": "SENZING_SUBCOMMAND",
    },
    "threads_per_print": {
        "default": 4,
        "env": "SENZING_THREADS_PER_PRINT",
        "cli": "threads-per-print"
    }
}

# Enumerate keys in 'configuration_locator' that should not be printed to the log.

keys_to_redact = [
    "password",
]

# -----------------------------------------------------------------------------
# Define argument parser
# -----------------------------------------------------------------------------


def get_parser():
    ''' Parse commandline arguments. '''

    subcommands = {
        'avro-to-rabbitmq': {
            "help": 'Read Avro file and send to RabbitMQ.',
            "argument_aspects": ["input-url", "avro", "rabbitmq"]
        },
        'avro-to-stdout': {
            "help": 'Read Avro file and print to STDOUT.',
            "argument_aspects": ["input-url", "avro", "stdout"]
        },
        'csv-to-rabbitmq': {
            "help": 'Read CSV file and send to RabbitMQ.',
            "argument_aspects": ["input-url", "csv", "rabbitmq"]
        },
        'csv-to-stdout': {
            "help": 'Read CSV file and print to STDOUT.',
            "argument_aspects": ["input-url", "csv", "stdout"]
        },
        'json-to-kafka': {
            "help": 'Read JSON file and send to Kafka.',
            "argument_aspects": ["input-url", "json", "kafka"]
        },
        'json-to-rabbitmq': {
            "help": 'Read JSON file and send to RabbitMQ.',
            "argument_aspects": ["input-url", "json", "rabbitmq"]
        },
        'json-to-stdout': {
            "help": 'Read JSON file and print to STDOUT.',
            "argument_aspects": ["input-url", "json", "stdout"]
        },
        'parquet-to-rababitmq': {
            "help": 'Read Parquet file and send to RabbitMQ.',
            "argument_aspects": ["input-url", "parquet", "rabbitmq"]
        },
        'parquet-to-stdout': {
            "help": 'Read Parquet file and print to STDOUT.',
            "argument_aspects": ["input-url", "parquet", "stdout"]
        },
        'sleep': {
            "help": 'Do nothing but sleep. For Docker testing.',
            "arguments": {
                "--sleep-time-in-seconds": {
                    "dest": "sleep_time_in_seconds",
                    "metavar": "SENZING_SLEEP_TIME_IN_SECONDS",
                    "help": "Sleep time in seconds. DEFAULT: 0 (infinite)"
                },
            },
        },
        'version': {
            "help": 'Print version of program.',
        },
        'docker-acceptance-test': {
            "help": 'For Docker acceptance testing.',
        },
    }

    # Define argument_aspects.

    argument_aspects = {
        "input-url": {
            "--default-data-source": {
                "dest": "default_data_source",
                "metavar": "SENZING_DEFAULT_DATA_SOURCE",
                "help": "Used when record does not have a `DATA_SOURCE` key. Default: None"
            },
            "--default-entity-type": {
                "dest": "default_entity_type",
                "metavar": "SENZING_DEFAULT_ENTITY_TYPE",
                "help": "Used when record does not have a `ENTITY_TYPE` key. Default: None"
            },
            "--input-url": {
                "dest": "input_url",
                "metavar": "SENZING_INPUT_URL",
                "help": "File/URL of input file. Default: None"
            },
            "--record-max": {
                "dest": "record_max",
                "metavar": "SENZING_RECORD_MAX",
                "help": "Highest record id. Default: None."
            },
            "--record-min": {
                "dest": "record_min",
                "metavar": "SENZING_RECORD_MIN",
                "help": "Lowest record id. Default: None"
            },
            "--threads-per-print": {
                "dest": "threads_per_print",
                "metavar": "SENZING_THREADS_PER_PRINT",
                "help": "Threads for print phase. Default: 4"
            },
        },
        "kafka": {
            "--kafka-bootstrap-server": {
                "dest": "kafka_bootstrap_server",
                "metavar": "SENZING_KAFKA_BOOTSTRAP_SERVER",
                "help": "Kafka bootstrap server. Default: localhost:9092"
            },
            "--kafka-topic": {
                "dest": "kafka_topic",
                "metavar": "SENZING_KAFKA_TOPIC",
                "help": "Kafka topic. Default: senzing-kafka-topic"
            },
        },
        "rabbitmq": {
            "--rabbitmq-host": {
                "dest": "rabbitmq_host",
                "metavar": "SENZING_RABBITMQ_HOST",
                "help": "RabbitMQ host. Default: localhost"
            },
            "--rabbitmq-port": {
                "dest": "rabbitmq_port",
                "metavar": "SENZING_RABBITMQ_PORT",
                "help": "RabbitMQ port. Default: 5672"
            },
            "--rabbitmq-queue": {
                "dest": "rabbitmq_queue",
                "metavar": "SENZING_RABBITMQ_QUEUE",
                "help": "RabbitMQ queue. Default: senzing-rabbitmq-queue"
            },
            "--rabbitmq-username": {
                "dest": "rabbitmq_username",
                "metavar": "SENZING_RABBITMQ_USERNAME",
                "help": "RabbitMQ username. Default: user"
            },
            "--rabbitmq-password": {
                "dest": "rabbitmq_password",
                "metavar": "SENZING_RABBITMQ_PASSWORD",
                "help": "RabbitMQ password. Default: bitnami"
            },
            "--rabbitmq-exchange": {
                "dest": "rabbitmq_exchange",
                "metavar": "SENZING_RABBITMQ_EXCHANGE",
                "help": "RabbitMQ exchange name. Default: empty string"
            },
        },
    }

    # Augment "subcommands" variable with arguments specified by aspects.

    for subcommand, subcommand_value in subcommands.items():
        if 'argument_aspects' in subcommand_value:
            for aspect in subcommand_value['argument_aspects']:
                if 'arguments' not in subcommands[subcommand]:
                    subcommands[subcommand]['arguments'] = {}
                arguments = argument_aspects.get(aspect, {})
                for argument, argument_value in arguments.items():
                    subcommands[subcommand]['arguments'][argument] = argument_value

    # Parse command line arguments.

    parser = argparse.ArgumentParser(prog="template-python.py", description="Example python skeleton. For more information, see https://github.com/Senzing/template-python")
    subparsers = parser.add_subparsers(dest='subcommand', help='Subcommands (SENZING_SUBCOMMAND):')

    for subcommand_key, subcommand_values in subcommands.items():
        subcommand_help = subcommand_values.get('help', "")
        subcommand_arguments = subcommand_values.get('arguments', {})
        subparser = subparsers.add_parser(subcommand_key, help=subcommand_help)
        for argument_key, argument_values in subcommand_arguments.items():
            subparser.add_argument(argument_key, **argument_values)

    return parser

# -----------------------------------------------------------------------------
# Message handling
# -----------------------------------------------------------------------------

# 1xx Informational (i.e. logging.info())
# 3xx Warning (i.e. logging.warning())
# 5xx User configuration issues (either logging.warning() or logging.err() for Client errors)
# 7xx Internal error (i.e. logging.error for Server errors)
# 9xx Debugging (i.e. logging.debug())


MESSAGE_INFO = 100
MESSAGE_WARN = 300
MESSAGE_ERROR = 700
MESSAGE_DEBUG = 900

message_dictionary = {
    "100": "senzing-" + SENZING_PRODUCT_ID + "{0:04d}I",
    "103": "Kafka topic: {0}; message: {1}; error: {2}; error: {3}",
    "104": "Records sent to Kafka: {0}",
    "105": "Records sent to STDOUT: {0}",
    "106": "Records sent to RabbitMQ: {0}",
    "120": "Sleeping for requested delay of {0} seconds.",
    "127": "Monitor: {0}",
    "129": "{0} is running.",
    "130": "{0} has exited.",
    "181": "Monitoring halted. No active workers.",
    "292": "Configuration change detected.  Old: {0} New: {1}",
    "293": "For information on warnings and errors, see https://github.com/Senzing/stream-loader#errors",
    "294": "Version: {0}  Updated: {1}",
    "295": "Sleeping infinitely.",
    "296": "Sleeping {0} seconds.",
    "297": "Enter {0}",
    "298": "Exit {0}",
    "299": "{0}",
    "300": "senzing-" + SENZING_PRODUCT_ID + "{0:04d}W",
    "404": "Buffer error: {0} for line #{1} '{2}'.",
    "405": "Kafka error: {0} for line #{1} '{2}'.",
    "406": "Not implemented error: {0} for line #{1} '{2}'.",
    "407": "Unknown kafka error: {0} for line #{1} '{2}'.",
    "408": "Kafka topic: {0}; message: {1}; error: {2}; error: {3}",
    "410": "Unknown RabbitMQ error when connecting: {0}.",
    "411": "Unknown RabbitMQ error when adding record to queue: {0} for line {1}.",
    "412": "Could not connect to RabbitMQ host at {1}. The host name maybe wrong, it may not be ready, or your credentials are incorrect. See the RabbitMQ log for more details.",
    "499": "{0}",
    "500": "senzing-" + SENZING_PRODUCT_ID + "{0:04d}E",
    "695": "Unknown database scheme '{0}' in database url '{1}'",
    "696": "Bad SENZING_SUBCOMMAND: {0}.",
    "697": "No processing done.",
    "698": "Program terminated with error.",
    "699": "{0}",
    "700": "senzing-" + SENZING_PRODUCT_ID + "{0:04d}E",
    "721": "Running low on workers.  May need to restart",
    "885": "License has expired.",
    "886": "G2Engine.addRecord() bad return code: {0}; JSON: {1}",
    "888": "G2Engine.addRecord() G2ModuleNotInitialized: {0}; JSON: {1}",
    "889": "G2Engine.addRecord() G2ModuleGenericException: {0}; JSON: {1}",
    "890": "G2Engine.addRecord() Exception: {0}; JSON: {1}",
    "891": "Original and new database URLs do not match. Original URL: {0}; Reconstructed URL: {1}",
    "892": "Could not initialize G2Product with '{0}'. Error: {1}",
    "893": "Could not initialize G2Hasher with '{0}'. Error: {1}",
    "894": "Could not initialize G2Diagnostic with '{0}'. Error: {1}",
    "895": "Could not initialize G2Audit with '{0}'. Error: {1}",
    "896": "Could not initialize G2ConfigMgr with '{0}'. Error: {1}",
    "897": "Could not initialize G2Config with '{0}'. Error: {1}",
    "898": "Could not initialize G2Engine with '{0}'. Error: {1}",
    "899": "{0}",
    "900": "senzing-" + SENZING_PRODUCT_ID + "{0:04d}D",
    "902": "Thread: {0} Added message to internal queue: {1}",
    "995": "Thread: {0} Using Class: {1}",
    "996": "Thread: {0} Using Mixin: {1}",
    "997": "Thread: {0} Using Thread: {1}",
    "998": "Debugging enabled.",
    "999": "{0}",
}


def message(index, *args):
    index_string = str(index)
    template = message_dictionary.get(index_string, "No message for index {0}.".format(index_string))
    return template.format(*args)


def message_generic(generic_index, index, *args):
    index_string = str(index)
    return "{0} {1}".format(message(generic_index, index), message(index, *args))


def message_info(index, *args):
    return message_generic(MESSAGE_INFO, index, *args)


def message_warning(index, *args):
    return message_generic(MESSAGE_WARN, index, *args)


def message_error(index, *args):
    return message_generic(MESSAGE_ERROR, index, *args)


def message_debug(index, *args):
    return message_generic(MESSAGE_DEBUG, index, *args)


def get_exception():
    ''' Get details about an exception. '''
    exception_type, exception_object, traceback = sys.exc_info()
    frame = traceback.tb_frame
    line_number = traceback.tb_lineno
    filename = frame.f_code.co_filename
    linecache.checkcache(filename)
    line = linecache.getline(filename, line_number, frame.f_globals)
    return {
        "filename": filename,
        "line_number": line_number,
        "line": line.strip(),
        "exception": exception_object,
        "type": exception_type,
        "traceback": traceback,
    }

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------


def get_configuration(args):
    ''' Order of precedence: CLI, OS environment variables, INI file, default. '''
    result = {}

    # Copy default values into configuration dictionary.

    for key, value in list(configuration_locator.items()):
        result[key] = value.get('default', None)

    # "Prime the pump" with command line args. This will be done again as the last step.

    for key, value in list(args.__dict__.items()):
        new_key = key.format(subcommand.replace('-', '_'))
        if value:
            result[new_key] = value

    # Copy OS environment variables into configuration dictionary.

    for key, value in list(configuration_locator.items()):
        os_env_var = value.get('env', None)
        if os_env_var:
            os_env_value = os.getenv(os_env_var, None)
            if os_env_value:
                result[key] = os_env_value

    # Copy 'args' into configuration dictionary.

    for key, value in list(args.__dict__.items()):
        new_key = key.format(subcommand.replace('-', '_'))
        if value:
            result[new_key] = value

    # Special case: subcommand from command-line

    if args.subcommand:
        result['subcommand'] = args.subcommand

    # Special case: Change boolean strings to booleans.

    booleans = ['debug']
    for boolean in booleans:
        boolean_value = result.get(boolean)
        if isinstance(boolean_value, str):
            boolean_value_lower_case = boolean_value.lower()
            if boolean_value_lower_case in ['true', '1', 't', 'y', 'yes']:
                result[boolean] = True
            else:
                result[boolean] = False

    # Special case: Change integer strings to integers.

    integers = [
        'delay_in_seconds',
        'kafka_poll_interval',
        'record_max',
        'record_min',
        'record_monitor',
        'sleep_time_in_seconds',
        'threads_per_print',
    ]
    for integer in integers:
        integer_string = result.get(integer)
        if integer_string:
            result[integer] = int(integer_string)

    # Initialize counters.

    counters = [
        'input_counter',
        'output_counter',
        'output_counter_reported',
    ]
    for counter in counters:
        result[counter] = 0

    return result


def validate_configuration(config):
    ''' Check aggregate configuration from commandline options, environment variables, config files, and defaults. '''

    user_warning_messages = []
    user_error_messages = []

    # Perform subcommand specific checking.

    subcommand = config.get('subcommand')

    if subcommand in ['task1']:

        if not config.get('example'):
            user_error_messages.append(message_error(414))

    # Log warning messages.

    for user_warning_message in user_warning_messages:
        logging.warning(user_warning_message)

    # Log error messages.

    for user_error_message in user_error_messages:
        logging.error(user_error_message)

    # Log where to go for help.

    if len(user_warning_messages) > 0 or len(user_error_messages) > 0:
        logging.info(message_info(293))

    # If there are error messages, exit.

    if len(user_error_messages) > 0:
        exit_error(697)


def redact_configuration(config):
    ''' Return a shallow copy of config with certain keys removed. '''
    result = config.copy()
    for key in keys_to_redact:
        try:
            result.pop(key)
        except:
            pass
    return result

# -----------------------------------------------------------------------------
# Utility functions
# -----------------------------------------------------------------------------


def bootstrap_signal_handler(signal, frame):
    sys.exit(0)


def create_signal_handler_function(args):
    ''' Tricky code.  Uses currying technique. Create a function for signal handling.
        that knows about "args".
    '''

    def result_function(signal_number, frame):
        logging.info(message_info(298, args))
        sys.exit(0)

    return result_function


def delay(config):
    delay_in_seconds = config.get('delay_in_seconds')
    if delay_in_seconds > 0:
        logging.info(message_info(120, delay_in_seconds))
        time.sleep(delay_in_seconds)


def entry_template(config):
    ''' Format of entry message. '''
    debug = config.get("debug", False)
    config['start_time'] = time.time()
    if debug:
        final_config = config
    else:
        final_config = redact_configuration(config)
    config_json = json.dumps(final_config, sort_keys=True)
    return message_info(297, config_json)


def exit_template(config):
    ''' Format of exit message. '''
    debug = config.get("debug", False)
    stop_time = time.time()
    config['stop_time'] = stop_time
    config['elapsed_time'] = stop_time - config.get('start_time', stop_time)
    if debug:
        final_config = config
    else:
        final_config = redact_configuration(config)
    config_json = json.dumps(final_config, sort_keys=True)
    return message_info(298, config_json)


def exit_error(index, *args):
    ''' Log error message and exit program. '''
    logging.error(message_error(index, *args))
    logging.error(message_error(698))
    sys.exit(1)


def exit_silently():
    ''' Exit program. '''
    sys.exit(0)

# -----------------------------------------------------------------------------
# Class: MonitorThread
# -----------------------------------------------------------------------------


class MonitorThread(threading.Thread):
    '''
    Periodically log operational metrics.
    '''

    def __init__(self, config=None, workers=None):
        threading.Thread.__init__(self)
        self.config = config
        self.workers = workers

    def run(self):
        '''Periodically monitor what is happening.'''

        # Show that thread is starting in the log.

        logging.info(message_info(129, threading.current_thread().name))

        # Initialize variables.

        last = {
            "input_counter": 0,
            "output_counter": 0,
        }

        # Define monitoring report interval.

        sleep_time_in_seconds = self.config.get('monitoring_period_in_seconds')

        # Sleep-monitor loop.

        active_workers = len(self.workers)
        for worker in self.workers:
            if not worker.is_alive():
                active_workers -= 1

        while active_workers > 0:

            # Tricky code.  Essentially this is an interruptible
            # time.sleep(sleep_time_in_seconds)

            interval_in_seconds = 5
            active_workers = len(self.workers)
            for step in range(1, sleep_time_in_seconds, interval_in_seconds):
                time.sleep(interval_in_seconds)
                active_workers = len(self.workers)
                for worker in self.workers:
                    if not worker.is_alive():
                        active_workers -= 1
                if active_workers == 0:
                    break;

            # Determine if we're running out of workers.

            if active_workers and (active_workers / float(len(self.workers))) < 0.5:
                logging.warning(message_warning(721))

            # Calculate times.

            now = time.time()
            uptime = now - self.config.get('start_time', now)

            # Construct and log monitor statistics.

            stats = {
                "uptime": int(uptime),
                "workers_total": len(self.workers),
                "workers_active": active_workers,
            }

            # Tricky code.  Avoid modifying dictionary in the loop.
            # i.e. "for key, value in last.items():" would loop infinitely
            # because of "last[key] = total".

            keys = last.keys()
            for key in keys:
                value = last.get(key)
                total = self.config.get(key)
                interval = total - value
                stats["{0}_total".format(key)] = total
                stats["{0}_interval".format(key)] = interval
                last[key] = total

            logging.info(message_info(127, json.dumps(stats, sort_keys=True)))
        logging.info(message_info(181))

# =============================================================================
# Mixins: Read*
#   Methods:
#   - read() - a Generator that produces one message per iteration
#   Classes:
#   - ReadFileCsvMixin - Read a local CSV file
#   - ReadFileMixin - Read from a local file
#   - ReadFileParquetMixin - Read a parquet file
#   - ReadQueueMixin - Read from an internal queue
# =============================================================================

# -----------------------------------------------------------------------------
# Class: ReadFileAvroMixin
# -----------------------------------------------------------------------------


class ReadFileAvroMixin():

    def __init__(self, config={}, *args, **kwargs):
        logging.debug(message_debug(996, threading.current_thread().name, "ReadFileAvroMixin"))
        self.input_url = config.get('input_url')
        self.record_min = config.get('record_min')
        self.record_max = config.get('record_max')
        self.counter = 0

    def read(self):
        with open(self.input_url, 'rb') as input_file:
            avro_reader = fastavro.reader(input_file)
            for record in avro_reader:
                self.counter += 1
                if self.record_min and self.counter < self.record_min:
                    continue
                if self.record_max and self.counter > self.record_max:
                    break
                yield record

# -----------------------------------------------------------------------------
# Class: ReadFileCsvMixin
# -----------------------------------------------------------------------------


class ReadFileCsvMixin():

    def __init__(self, config={}, *args, **kwargs):
        logging.debug(message_debug(996, threading.current_thread().name, "ReadFileCsvMixin"))
        self.input_url = config.get('input_url')
        self.record_min = config.get('record_min')
        self.record_max = config.get('record_max')
        self.counter = 0

    def read(self):
        data_frame = pandas.read_csv(self.input_url, skipinitialspace=True)
        for row in data_frame.to_dict(orient="records"):
            self.counter += 1
            if self.record_min and self.counter < self.record_min:
                continue
            if self.record_max and self.counter > self.record_max:
                break
            assert type(row) == dict
            yield row

# -----------------------------------------------------------------------------
# Class: ReadFileMixin
# -----------------------------------------------------------------------------


class ReadFileMixin():

    def __init__(self, config={}, *args, **kwargs):
        logging.debug(message_debug(996, threading.current_thread().name, "ReadFileMixin"))
        self.input_url = config.get('input_url')
        self.record_min = config.get('record_min')
        self.record_max = config.get('record_max')
        self.counter = 0

    def read(self):
        with open(self.input_url, 'r') as input_file:
            for line in input_file:
                self.counter += 1
                if self.record_min and self.counter < self.record_min:
                    continue
                if self.record_max and self.counter > self.record_max:
                    break
                line = line.strip()
                if not line:
                    continue
                assert isinstance(line, str)
                yield line

# -----------------------------------------------------------------------------
# Class: ReadFileParquetMixin
# -----------------------------------------------------------------------------


class ReadFileParquetMixin():

    def __init__(self, config={}, *args, **kwargs):
        logging.debug(message_debug(996, threading.current_thread().name, "ReadFileParquetMixin"))
        self.input_url = config.get('input_url')
        self.record_min = config.get('record_min')
        self.record_max = config.get('record_max')
        self.counter = 0

    def read(self):
        data_frame = pandas.read_parquet(self.input_url)
        for row in data_frame.to_dict(orient="records"):
            self.counter += 1
            if self.record_min and self.counter < self.record_min:
                continue
            if self.record_max and self.counter > self.record_max:
                break
            assert type(row) == dict
            yield row

# -----------------------------------------------------------------------------
# Class: ReadQueueMixin
# -----------------------------------------------------------------------------


class ReadQueueMixin():

    def __init__(self, read_queue=None, *args, **kwargs):
        logging.debug(message_debug(996, threading.current_thread().name, "ReadQueueMixin"))
        self.read_queue = read_queue

    def read(self):
        while True:
            message = self.read_queue.get()

            # Tricky code. If end-of-task,
            # repeat message for next queue consumer thread.

            if message == QUEUE_SENTINEL:
                self.read_queue.put(QUEUE_SENTINEL)
                break;

            # Yield message.

            yield message

# -----------------------------------------------------------------------------
# Class: ReadUrlAvroMixin
# -----------------------------------------------------------------------------


class ReadUrlAvroMixin():

    def __init__(self, config={}, *args, **kwargs):
        logging.debug(message_debug(996, threading.current_thread().name, "ReadFileAvroMixin"))
        self.input_url = config.get('input_url')
        self.record_min = config.get('record_min')
        self.record_max = config.get('record_max')
        self.counter = 0

    def read(self):
        with urllib.request.urlopen(self.input_url) as input_file:
            avro_reader = fastavro.reader(input_file)
            for record in avro_reader:
                self.counter += 1
                if self.record_min and self.counter < self.record_min:
                    continue
                if self.record_max and self.counter > self.record_max:
                    break
                yield record

# -----------------------------------------------------------------------------
# Class: ReadUrlMixin
# -----------------------------------------------------------------------------


class ReadUrlMixin():

    def __init__(self, config={}, *args, **kwargs):
        logging.debug(message_debug(996, threading.current_thread().name, "ReadUrlMixin"))
        self.input_url = config.get('input_url')
        self.record_min = config.get('record_min')
        self.record_max = config.get('record_max')
        self.counter = 0

    def read(self):

        data = urllib.request.urlopen(self.input_url, timeout=5)
        for line in data:
            self.counter += 1
            if self.record_min and self.counter < self.record_min:
                continue
            if self.record_max and self.counter > self.record_max:
                break
            line = line.strip()
            if not line:
                continue
            result = json.loads(line)
            assert isinstance(result, dict)
            yield result

# =============================================================================
# Mixins: Evaluate*
#   Methods:
#   - evaluate(message) -> transformed-message
#   Classes:
#   - EvaluateDictToJsonMixin - Transform Python dictionary to JSON string
#   - EvaluateJsonToDictMixin - Transform JSON string to Python dictionary
#   - EvaluateNullObjectMixin - Simply pass on the message
#   - EvaluateMakeSerializeableDictMixin - Make dictionary serializeable
# =============================================================================

# -----------------------------------------------------------------------------
# Class: EvaluateDictToJsonMixin
# -----------------------------------------------------------------------------


class EvaluateDictToJsonMixin():

    def __init__(self, *args, **kwargs):
        logging.debug(message_debug(996, threading.current_thread().name, "EvaluateDictToJsonMixin"))

    def evaluate(self, message):
        return json.dumps(message)

# -----------------------------------------------------------------------------
# Class: EvaluateJsonToDictMixin
# -----------------------------------------------------------------------------


class EvaluateJsonToDictMixin():

    def __init__(self, *args, **kwargs):
        logging.debug(message_debug(996, threading.current_thread().name, "EvaluateJsonToDictMixin"))

    def evaluate(self, message):
        return json.loads(message)

# -----------------------------------------------------------------------------
# Class: EvaluateNullObjectMixin
# -----------------------------------------------------------------------------


class EvaluateNullObjectMixin():

    def __init__(self, *args, **kwargs):
        logging.debug(message_debug(996, threading.current_thread().name, "EvaluateDictToJsonMixin"))

    def evaluate(self, message):
        return message

# -----------------------------------------------------------------------------
# Class: EvaluateMakeSerializeableDictMixin
# -----------------------------------------------------------------------------


class EvaluateMakeSerializeableDictMixin():

    def __init__(self, *args, **kwargs):
        logging.debug(message_debug(996, threading.current_thread().name, "EvaluateMakeSerializeableDictMixin"))

    def evaluate(self, message):
        new_message = {}
        for key, value in message.items():
            new_message[key] = str(value)
            try:
                if value.isnumeric():
                    new_message[key] = value
            except:
                pass
        return new_message

# =============================================================================
# Mixins: Print*
#   Methods:
#   - close()
#   - print()
#   Classes:
#   - PrintQueueMixin - Send to internal queue
#   - PrintStdoutMixin - Send to STDOUT
# =============================================================================

# -----------------------------------------------------------------------------
# Class: PrintKafkaMixin
# -----------------------------------------------------------------------------


class PrintKafkaMixin():

    def __init__(self, config={}, *args, **kwargs):
        logging.debug(message_debug(996, threading.current_thread().name, "PrintKafkaMixin"))
        self.config = config
        self.kafka_topic = config.get('kafka_topic')
        self.record_monitor = config.get("record_monitor")
        self.kafka_poll_interval = config.get("kafka_poll_interval")

        kafka_configuration = {
            'bootstrap.servers':  config.get('kafka_bootstrap_server')
        }
        if config.get('kafka_group_id'):
            kafka_configuration['group.id'] = config.get('kafka_group_id')

        self.kafka_producer = confluent_kafka.Producer(kafka_configuration)

    def on_kafka_delivery(self, error, message):
        logging.debug(message_debug(103, message.topic(), message.value(), message.error(), error))
        if error is not None:
            logging.warning(message_warning(408, message.topic(), message.value(), message.error(), error))

    def print(self, message):
        assert isinstance(message, str)

        # Send message to Kafka.

        try:
            self.kafka_producer.produce(
                self.kafka_topic,
                message,
                on_delivery=self.on_kafka_delivery
            )
        except BufferError as err:
            logging.warning(message_warning(404, err, message))
        except confluent_kafka.KafkaException as err:
            logging.warning(message_warning(405, err, message))
        except NotImplemented as err:
            logging.warning(message_warning(406, err, message))
        except:
            logging.warning(message_warning(407, err, message))

        # Log progress. Using a "cheap" serialization technique.

        output_counter = self.config.get('output_counter')
        if output_counter % self.record_monitor == 0:
            if output_counter != self.config.get('output_counter_reported'):
                self.config['output_counter_reported'] = output_counter
                logging.info(message_debug(104, output_counter))

        # Poll Kafka for callbacks.

        if output_counter % self.kafka_poll_interval == 0:
            self.kafka_producer.poll(0)

    def close(self):
        self.kafka_producer.flush()

# -----------------------------------------------------------------------------
# Class: PrintRabbitmqMixin
# -----------------------------------------------------------------------------


class PrintRabbitmqMixin():

    def __init__(self, config={}, *args, **kwargs):
        logging.debug(message_debug(996, threading.current_thread().name, "PrintRabbitmqMixin"))

        rabbitmq_delivery_mode = 2
        rabbitmq_host = config.get("rabbitmq_host")
        rabbitmq_port = config.get("rabbitmq_port")
        rabbitmq_username = config.get("rabbitmq_username")
        rabbitmq_password = config.get("rabbitmq_password")
        self.rabbitmq_exchange = config.get("rabbitmq_exchange")
        self.rabbitmq_queue = config.get("rabbitmq_queue")
        self.record_monitor = config.get("record_monitor")
        self.record_monitor = config.get("record_monitor")

        # Construct Pika objects.

        self.rabbitmq_properties = pika.BasicProperties(
            delivery_mode=rabbitmq_delivery_mode
        )
        credentials = pika.PlainCredentials(
            username=rabbitmq_username,
            password=rabbitmq_password
        )
        rabbitmq_connection_parameters = pika.ConnectionParameters(
            host=rabbitmq_host,
            port=rabbitmq_port,
            credentials=credentials
        )

        # Open connection to RabbitMQ.

        try:
            self.connection = pika.BlockingConnection(rabbitmq_connection_parameters)
            self.channel = self.connection.channel()
            self.channel.queue_declare(queue=self.rabbitmq_queue)
        except (pika.exceptions.AMQPConnectionError) as err:
            exit_error(412, err, rabbitmq_host)
        except BaseException as err:
            exit_error(410, err)

    def print(self, message):
        assert isinstance(message, str)

        # Send message to RabbitMQ.

        try:
            self.channel.basic_publish(
                exchange=self.rabbitmq_exchange,
                routing_key=self.rabbitmq_queue,
                body=message,
                properties=self.rabbitmq_properties
            )
        except BaseException as err:
            logging.warn(message_warning(411, err, message))

        # Log progress. Using a "cheap" serialization technique.

        output_counter = self.config.get('output_counter')
        if output_counter % self.record_monitor == 0:
            if output_counter != self.config.get('output_counter_reported'):
                self.config['output_counter_reported'] = output_counter
                logging.info(message_debug(106, output_counter))

    def close(self):
        self.connection.close()

# -----------------------------------------------------------------------------
# Class: PrintQueueMixin
# -----------------------------------------------------------------------------


class PrintQueueMixin():

    def __init__(self, print_queue=None, *args, **kwargs):
        logging.debug(message_debug(996, threading.current_thread().name, "PrintQueueMixin"))
        self.print_queue = print_queue

    def print(self, message):
        assert isinstance(message, dict)
        self.print_queue.put(message)

    def close(self):
        self.print_queue.put(QUEUE_SENTINEL)

# -----------------------------------------------------------------------------
# Class: PrintStdoutMixin
# -----------------------------------------------------------------------------


class PrintStdoutMixin():

    def __init__(self, *args, **kwargs):
        logging.debug(message_debug(996, threading.current_thread().name, "PrintStdoutMixin"))
        config = kwargs.get("config", {})
        self.record_monitor = config.get("record_monitor")
        self.counter = 0

    def print(self, message):
        self.counter += 1
        assert type(message) == str
        print(message)
        if self.counter % self.record_monitor == 0:
            logging.info(message_debug(105, counter))

    def close(self):
        pass

# =============================================================================
# Threads: *Thread
#   Methods:
#   - run
#   Classes:
#   - ReadEvaluatePrintLoopThread - Simple REPL
# =============================================================================

# -----------------------------------------------------------------------------
# Class: ReadEvaluatePrintLoopThread
# -----------------------------------------------------------------------------


class ReadEvaluatePrintLoopThread(threading.Thread):

    def __init__(self, config=None, counter_name=None, *args, **kwargs):
        threading.Thread.__init__(self)
        logging.debug(message_debug(997, threading.current_thread().name, "ReadEvaluatePrintLoopThread"))
        self.config = config
        self.counter_name = counter_name

    def run(self):
        '''Read-Evaluate-Print Loop (REPL).'''

        # Show that thread is starting in the log.

        logging.info(message_info(129, threading.current_thread().name))

        # Read-Evaluate-Print Loop  (REPL)

        for message in self.read():
            logging.debug(message_debug(902, threading.current_thread().name, self.counter_name, message))
            self.print(self.evaluate(message))
            self.config[self.counter_name] += 1

        self.close()

        # Log message for thread exiting.

        logging.info(message_info(130, threading.current_thread().name))

# =============================================================================
# Filter* classes created with mixins
# =============================================================================


class FilterFileAvroToDictQueueThread(ReadEvaluatePrintLoopThread, ReadFileAvroMixin, EvaluateNullObjectMixin, PrintQueueMixin):

    def __init__(self, *args, **kwargs):
        logging.debug(message_debug(997, threading.current_thread().name, "FilterFileAvroToDictQueueThread"))
        for base in type(self).__bases__:
            base.__init__(self, *args, **kwargs)


class FilterFileCsvToDictQueueThread(ReadEvaluatePrintLoopThread, ReadFileCsvMixin, EvaluateNullObjectMixin, PrintQueueMixin):

    def __init__(self, *args, **kwargs):
        logging.debug(message_debug(997, threading.current_thread().name, "FilterFileCsvToDictQueueThread"))
        for base in type(self).__bases__:
            base.__init__(self, *args, **kwargs)


class FilterFileJsonToDictQueueThread(ReadEvaluatePrintLoopThread, ReadFileMixin, EvaluateJsonToDictMixin, PrintQueueMixin):

    def __init__(self, *args, **kwargs):
        logging.debug(message_debug(997, threading.current_thread().name, "FilterFileJsonToDictQueueThread"))
        for base in type(self).__bases__:
            base.__init__(self, *args, **kwargs)


class FilterFileParquetToDictQueueThread(ReadEvaluatePrintLoopThread, ReadFileParquetMixin, EvaluateMakeSerializeableDictMixin, PrintQueueMixin):

    def __init__(self, *args, **kwargs):
        logging.debug(message_debug(997, threading.current_thread().name, "FilterFileParquetToDictQueueThread"))
        for base in type(self).__bases__:
            base.__init__(self, *args, **kwargs)


class FilterQueueDictToJsonKafkaThread(ReadEvaluatePrintLoopThread, ReadQueueMixin, EvaluateDictToJsonMixin, PrintKafkaMixin):

    def __init__(self, *args, **kwargs):
        logging.debug(message_debug(997, threading.current_thread().name, "FilterQueueDictToJsonKafkaThread"))
        for base in type(self).__bases__:
            base.__init__(self, *args, **kwargs)


class FilterQueueDictToJsonRabbitmqThread(ReadEvaluatePrintLoopThread, ReadQueueMixin, EvaluateDictToJsonMixin, PrintRabbitmqMixin):

    def __init__(self, *args, **kwargs):
        logging.debug(message_debug(997, threading.current_thread().name, "FilterQueueDictToJsonRabbitmqThread"))
        for base in type(self).__bases__:
            base.__init__(self, *args, **kwargs)


class FilterQueueDictToJsonStdoutThread(ReadEvaluatePrintLoopThread, ReadQueueMixin, EvaluateDictToJsonMixin, PrintStdoutMixin):

    def __init__(self, *args, **kwargs):
        logging.debug(message_debug(997, threading.current_thread().name, "FilterQueueDictToJsonStdoutThread"))
        for base in type(self).__bases__:
            base.__init__(self, *args, **kwargs)


class FilterUrlAvroToDictQueueThread(ReadEvaluatePrintLoopThread, ReadUrlAvroMixin, EvaluateNullObjectMixin, PrintQueueMixin):

    def __init__(self, *args, **kwargs):
        logging.debug(message_debug(997, threading.current_thread().name, "FilterUrlAvroToDictQueueThread"))
        for base in type(self).__bases__:
            base.__init__(self, *args, **kwargs)


class FilterUrlJsonToDictQueueThread(ReadEvaluatePrintLoopThread, ReadUrlMixin, EvaluateNullObjectMixin, PrintQueueMixin):

    def __init__(self, *args, **kwargs):
        logging.debug(message_debug(997, threading.current_thread().name, "FilterUrlJsonToDictQueueThread"))
        for base in type(self).__bases__:
            base.__init__(self, *args, **kwargs)

# -----------------------------------------------------------------------------
# *_processor
# -----------------------------------------------------------------------------

def pipeline_runner(
    args=None,
    options_to_defaults_map={},
    pipeline=[],
    monitor_thread=None,
):

    # Get context from CLI, environment variables, and ini files.

    config = get_configuration(args)
    validate_configuration(config)

    # If configuration values not specified, use defaults.

    for key, value in options_to_defaults_map.items():
        if not config.get(key):
            config[key] = config.get(value)

    # Prolog.

    logging.info(entry_template(config))

    # If requested, delay start.

    delay(config)

    # Pull values from configuration.

    default_queue_maxsize = config.get('read_queue_maxsize')

    # Create threads for master process.

    threads = []
    input_queue = None

    # Create pipeline segments.

    for filter in pipeline:

        # Get metadata about the filter.

        filter_class = filter.get("class")
        filter_threads = filter.get("threads", 1)
        filter_queue_max_size = filter.get("queue_max_size", default_queue_maxsize)
        filter_counter_name = filter.get("counter_name")
        filter_delay = filter.get("delay", 1)

        # Give prior filter a head start

        time.sleep(filter_delay)

        # Create internal Queue.

        output_queue = multiprocessing.Queue(filter_queue_max_size)

        # Start threads.

        for i in range(0, filter_threads):
            thread = filter_class(
                config=config,
                counter_name=filter_counter_name,
                input_queue=input_queue,
                output_queue=output_queue,
            )
            thread.name = "Process-0-{0}-{1}".format(thread.__class__.__name__, i)
            threads.append(thread)
            thread.start()

        # Prepare for next filter.

        input_queue = output_queue


    # Add a monitoring thread.

    adminThreads = []

    if monitor_thread:
        thread = monitor_thread(
            config=config,
            workers=threads,
        )
        thread.name = "Process-0-{0}-0".format(thread.__class__.__name__)
        adminThreads.append(thread)
        thread.start()

    # Collect inactive threads.

    for thread in threads:
        thread.join()
    for thread in adminThreads:
        thread.join()

    # Epilog.

    logging.info(exit_template(config))

def pipeline_read_write(
    args=None,
    options_to_defaults_map={},
    read_thread=None,
    write_thread=None,
    monitor_thread=None,
):

    # Get context from CLI, environment variables, and ini files.

    config = get_configuration(args)
    validate_configuration(config)

    # If configuration values not specified, use defaults.

    for key, value in options_to_defaults_map.items():
        if not config.get(key):
            config[key] = config.get(value)

    # Prolog.

    logging.info(entry_template(config))

    # If requested, delay start.

    delay(config)

    # Pull values from configuration.

    threads_per_print = config.get('threads_per_print')
    read_queue_maxsize = config.get('read_queue_maxsize')

    # Create internal Queue.

    read_queue = multiprocessing.Queue(read_queue_maxsize)

    # Create threads for master process.

    threads = []

    # Add a single thread for reading from source and placing on internal queue.

    if read_thread:
        thread = read_thread(
            config=config,
            counter_name="input_counter",
            print_queue=read_queue
        )
        thread.name = "Process-0-{0}-0".format(thread.__class__.__name__)
        threads.append(thread)
        thread.start()

    # Let read thread get a head start.

    time.sleep(5)

    # Add a number of threads for reading from source queue writing to "sink".

    if write_thread:
        for i in range(0, threads_per_print):
            thread = write_thread(
                config=config,
                counter_name="output_counter",
                read_queue=read_queue,
            )
            thread.name = "Process-0-{0}-{1}".format(thread.__class__.__name__, i)
            threads.append(thread)
            thread.start()

    # Add a monitoring thread.

    adminThreads = []

    if monitor_thread:
        thread = monitor_thread(
            config=config,
            workers=threads,
        )
        thread.name = "Process-0-{0}-0".format(thread.__class__.__name__)
        adminThreads.append(thread)
        thread.start()

    # Collect inactive threads.

    for thread in threads:
        thread.join()
    for thread in adminThreads:
        thread.join()

    # Epilog.

    logging.info(exit_template(config))

# -----------------------------------------------------------------------------
# dohelper_* functions
#   Common function signature: do_XXX(args)
# -----------------------------------------------------------------------------


def dohelper_avro(args, write_thread):
    ''' Read file of AVRO, print to write_thread. '''

    # Get context variables.

    config = get_configuration(args)
    input_url = config.get("input_url")
    parsed_file_name = urllib.parse.urlparse(input_url)

    # Determine Read thread.

    read_thread = FilterFileAvroToDictQueueThread
    if parsed_file_name.scheme in ['http', 'https']:
        read_thread = FilterUrlAvroToDictQueueThread

    # Cascading defaults.

    options_to_defaults_map = {}

    # Run pipeline.

    pipeline_read_write(
        args=args,
        options_to_defaults_map=options_to_defaults_map,
        read_thread=read_thread,
        write_thread=write_thread,
        monitor_thread=MonitorThread
    )


def dohelper_csv(args, write_thread):
    ''' Read file of CSV, print to write_thread. '''

    # Get context variables.

    config = get_configuration(args)
    input_url = config.get("input_url")
    parsed_file_name = urllib.parse.urlparse(input_url)

    # Determine Read thread.

    read_thread = FilterFileCsvToDictQueueThread

    # Cascading defaults.

    options_to_defaults_map = {}

    # Run pipeline.

    pipeline_read_write(
        args=args,
        options_to_defaults_map=options_to_defaults_map,
        read_thread=read_thread,
        write_thread=write_thread,
        monitor_thread=MonitorThread
    )


def dohelper_json(args, write_thread):
    ''' Read file of JSON, print to write_thread. '''

    # Get context variables.

    config = get_configuration(args)
    input_url = config.get("input_url")
    parsed_file_name = urllib.parse.urlparse(input_url)

    # Determine Read thread.

    read_thread = FilterFileJsonToDictQueueThread  # Default.
    if parsed_file_name.scheme in ['http', 'https']:
        read_thread = FilterUrlJsonToDictQueueThread

    # Cascading defaults.

    options_to_defaults_map = {}

    # Run pipeline.

    pipeline_read_write(
        args=args,
        options_to_defaults_map=options_to_defaults_map,
        read_thread=read_thread,
        write_thread=write_thread,
        monitor_thread=MonitorThread
    )


def dohelper_parquet(args, write_thread):
    ''' Read file of Parquet, print to write_thread. '''

    # Get context variables.

    config = get_configuration(args)
    input_url = config.get("input_url")
    parsed_file_name = urllib.parse.urlparse(input_url)

    # Determine Read thread.

    read_thread = FilterFileParquetToDictQueueThread

    # Cascading defaults.

    options_to_defaults_map = {}

    # Run pipeline.

    pipeline_read_write(
        args=args,
        options_to_defaults_map=options_to_defaults_map,
        read_thread=read_thread,
        write_thread=write_thread,
        monitor_thread=MonitorThread
    )

# -----------------------------------------------------------------------------
# do_* functions
#   Common function signature: do_XXX(args)
# -----------------------------------------------------------------------------


def do_avro_to_kafka(args):
    ''' Read file of JSON, print to Kafka. '''
    write_thread = FilterQueueDictToJsonKafkaThread
    dohelper_avro(args, write_thread)


def do_avro_to_rabbitmq(args):
    ''' Read file of JSON, print to RabbitMQ. '''
    write_thread = FilterQueueDictToJsonRabbitmqThread
    dohelper_avro(args, write_thread)


def do_avro_to_stdout(args):
    ''' Read file of AVRO, print to STDOUT. '''
    write_thread = FilterQueueDictToJsonStdoutThread
    dohelper_avro(args, write_thread)


def do_csv_to_kafka(args):
    ''' Read file of CSV, print to Kafka. '''
    write_thread = FilterQueueDictToJsonKafkaThread
    dohelper_csv(args, write_thread)


def do_csv_to_rabbitmq(args):
    ''' Read file of CSV, print to RabbitMQ. '''
    write_thread = FilterQueueDictToJsonRabbitmqThread
    dohelper_csv(args, write_thread)


def do_csv_to_stdout(args):
    ''' Read file of CSV, print to STDOUT. '''
    write_thread = FilterQueueDictToJsonStdoutThread
    dohelper_csv(args, write_thread)


def do_docker_acceptance_test(args):
    ''' For use with Docker acceptance testing. '''

    # Get context from CLI, environment variables, and ini files.

    config = get_configuration(args)

    # Prolog.

    logging.info(entry_template(config))

    # Epilog.

    logging.info(exit_template(config))


def do_json_to_kafka(args):
    ''' Read file of JSON, print to Kafka. '''
    write_thread = FilterQueueDictToJsonKafkaThread
    dohelper_json(args, write_thread)


def do_json_to_rabbitmq(args):
    ''' Read file of JSON, print to RabbitMQ. '''
    write_thread = FilterQueueDictToJsonRabbitmqThread
    dohelper_json(args, write_thread)


def do_json_to_stdout(args):
    ''' Read file of JSON, print to STDOUT. '''
    write_thread = FilterQueueDictToJsonStdoutThread
    dohelper_json(args, write_thread)


def do_parquet_to_kafka(args):
    ''' Read file of Parquet, print to Kafka. '''
    write_thread = FilterQueueDictToJsonKafkaThread
    dohelper_parquet(args, write_thread)


def do_parquet_to_rabbitmq(args):
    ''' Read file of Parquet, print to RabbitMQ. '''
    write_thread = FilterQueueDictToJsonRabbitmqThread
    dohelper_parquet(args, write_thread)


def do_parquet_to_stdout(args):
    ''' Read file of Parquet, print to STDOUT. '''
    write_thread = FilterQueueDictToJsonStdoutThread
    dohelper_parquet(args, write_thread)


def do_sleep(args):
    ''' Sleep.  Used for debugging. '''

    # Get context from CLI, environment variables, and ini files.

    config = get_configuration(args)

    # Prolog.

    logging.info(entry_template(config))

    # Pull values from configuration.

    sleep_time_in_seconds = config.get('sleep_time_in_seconds')

    # Sleep

    if sleep_time_in_seconds > 0:
        logging.info(message_info(296, sleep_time_in_seconds))
        time.sleep(sleep_time_in_seconds)

    else:
        sleep_time_in_seconds = 3600
        while True:
            logging.info(message_info(295))
            time.sleep(sleep_time_in_seconds)

    # Epilog.

    logging.info(exit_template(config))


def do_version(args):
    ''' Log version information. '''

    logging.info(message_info(294, __version__, __updated__))

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


if __name__ == "__main__":

    # Configure logging. See https://docs.python.org/2/library/logging.html#levels

    log_level_map = {
        "notset": logging.NOTSET,
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "fatal": logging.FATAL,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL
    }

    log_level_parameter = os.getenv("SENZING_LOG_LEVEL", "info").lower()
    log_level = log_level_map.get(log_level_parameter, logging.INFO)
    logging.basicConfig(format=log_format, level=log_level)
    logging.debug(message_debug(998))

    # Trap signals temporarily until args are parsed.

    signal.signal(signal.SIGTERM, bootstrap_signal_handler)
    signal.signal(signal.SIGINT, bootstrap_signal_handler)

    # Parse the command line arguments.

    subcommand = os.getenv("SENZING_SUBCOMMAND", None)
    parser = get_parser()
    if len(sys.argv) > 1:
        args = parser.parse_args()
        subcommand = args.subcommand
    elif subcommand:
        args = argparse.Namespace(subcommand=subcommand)
    else:
        parser.print_help()
        if len(os.getenv("SENZING_DOCKER_LAUNCHED", "")):
            subcommand = "sleep"
            args = argparse.Namespace(subcommand=subcommand)
            do_sleep(args)
        exit_silently()

    # Catch interrupts. Tricky code: Uses currying.

    signal_handler = create_signal_handler_function(args)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Transform subcommand from CLI parameter to function name string.

    subcommand_function_name = "do_{0}".format(subcommand.replace('-', '_'))

    # Test to see if function exists in the code.

    if subcommand_function_name not in globals():
        logging.warning(message_warning(696, subcommand))
        parser.print_help()
        exit_silently()

    # Tricky code for calling function based on string.

    globals()[subcommand_function_name](args)
