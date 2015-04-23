#!/usr/bin/env python
from __future__ import print_function
__metaclass__ = type


from argparse import ArgumentParser
from contextlib import contextmanager
import glob
import logging
import os
import random
import re
import string
import subprocess
import sys
from time import sleep
import json
import shutil

import get_ami
from jujuconfig import (
    get_jenv_path,
    get_juju_home,
    translate_to_env,
)
from jujupy import (
    EnvJujuClient,
    get_local_root,
    SimpleEnvironment,
    temp_bootstrap_env,
)
from substrate import (
    LIBVIRT_DOMAIN_RUNNING,
    start_libvirt_domain,
    stop_libvirt_domain,
    verify_libvirt_domain,
)
from utility import (
    configure_logging,
    ensure_deleted,
    PortTimeoutError,
    print_now,
    until_timeout,
    wait_for_port,
)


def prepare_environment(client, already_bootstrapped, machines):
    """Prepare an environment for deployment.

    As well as bootstrapping, this ensures the correct agent version is in
    use.

    :param client: An EnvJujuClient to prepare the environment of.
    :param already_bootstrapped: If true, the environment is already
        bootstrapped.
    :param machines: A list of machines to add to the environment.
    """
    if sys.platform == 'win32':
        # Ensure OpenSSH is never in the path for win tests.
        sys.path = [p for p in sys.path if 'OpenSSH' not in p]
    if not already_bootstrapped:
        client.bootstrap()
    agent_version = client.get_matching_agent_version()
    for ignored in until_timeout(30):
        agent_versions = client.get_status().get_agent_versions()
        if 'unknown' not in agent_versions and len(agent_versions) == 1:
            break
    if agent_versions.keys() != [agent_version]:
        print("Current versions: %s" % ', '.join(agent_versions.keys()))
        client.juju('upgrade-juju', ('--version', agent_version))
    client.wait_for_version(client.get_matching_agent_version())
    for machine in machines:
        client.juju('add-machine', ('ssh:' + machine,))
    return client


def destroy_environment(client, instance_tag):
    client.destroy_environment()
    if (client.env.config['type'] == 'manual'
            and 'AWS_ACCESS_KEY' in os.environ):
        destroy_job_instances(instance_tag)


def destroy_job_instances(job_name):
    instances = list(get_job_instances(job_name))
    if len(instances) == 0:
        return
    subprocess.check_call(['euca-terminate-instances'] + instances)


def parse_euca(euca_output):
    for line in euca_output.splitlines():
        fields = line.split('\t')
        if fields[0] != 'INSTANCE':
            continue
        yield fields[1], fields[3]


def run_instances(count, job_name):
    environ = dict(os.environ)
    ami = get_ami.query_ami("precise", "amd64")
    command = [
        'euca-run-instances', '-k', 'id_rsa', '-n', '%d' % count,
        '-t', 'm1.large', '-g', 'manual-juju-test', ami]
    run_output = subprocess.check_output(command, env=environ).strip()
    machine_ids = dict(parse_euca(run_output)).keys()
    for remaining in until_timeout(300):
        try:
            names = dict(describe_instances(machine_ids, env=environ))
            if '' not in names.values():
                subprocess.check_call(
                    ['euca-create-tags', '--tag', 'job_name=%s' % job_name]
                    + machine_ids, env=environ)
                return names.items()
        except subprocess.CalledProcessError:
            subprocess.call(['euca-terminate-instances'] + machine_ids)
            raise
        sleep(1)


def deploy_dummy_stack(client, charm_prefix):
    """"Deploy a dummy stack in the specified environment.
    """
    client.deploy(charm_prefix + 'dummy-source')
    token = get_random_string()
    client.juju('set', ('dummy-source', 'token=%s' % token))
    client.deploy(charm_prefix + 'dummy-sink')
    client.juju('add-relation', ('dummy-source', 'dummy-sink'))
    client.juju('expose', ('dummy-sink',))
    if client.env.kvm:
        # A single virtual machine may need up to 30 minutes before
        # "apt-get update" and other initialisation steps are
        # finished; two machines initializing concurrently may
        # need even 40 minutes.
        client.wait_for_started(3600)
    else:
        client.wait_for_started()
    check_token(client, token)


GET_TOKEN_SCRIPT = """
        for x in $(seq 120); do
          if [ -f /var/run/dummy-sink/token ]; then
            if [ "$(cat /var/run/dummy-sink/token)" != "" ]; then
              break
            fi
          fi
          sleep 1
        done
        cat /var/run/dummy-sink/token
    """


def check_token(client, token):
    # Wait up to 120 seconds for token to be created.
    # Utopic is slower, maybe because the devel series gets more
    # package updates.
    logging.info('Retrieving token.')
    try:
        result = client.get_juju_output('ssh', 'dummy-sink/0',
                                        GET_TOKEN_SCRIPT)
    except subprocess.CalledProcessError as err:
        print("WARNING: juju ssh failed: {}".format(str(err)))
        print("Falling back to ssh.")
        dummy_sink = client.get_status().status['services']['dummy-sink']
        dummy_sink_ip = dummy_sink['units']['dummy-sink/0']['public-address']
        user_at_host = 'ubuntu@{}'.format(dummy_sink_ip)
        result = subprocess.check_output(
            ['ssh', user_at_host,
             '-o', 'UserKnownHostsFile /dev/null',
             '-o', 'StrictHostKeyChecking no',
             'cat /var/run/dummy-sink/token'])
    result = re.match(r'([^\n\r]*)\r?\n?', result).group(1)
    if result != token:
        raise ValueError('Token is %r' % result)


def get_random_string():
    allowed_chars = string.ascii_uppercase + string.digits
    return ''.join(random.choice(allowed_chars) for n in range(20))


def dump_env_logs(client, bootstrap_host, directory, host_id=None,
                  jenv_path=None):
    machine_addrs = get_machines_for_logs(client, bootstrap_host)

    for machine_id, addr in machine_addrs.iteritems():
        logging.info("Retrieving logs for machine-%s", machine_id)
        machine_directory = os.path.join(directory, machine_id)
        os.mkdir(machine_directory)
        local_state_server = client.env.local and machine_id == '0'
        dump_logs(client, addr, machine_directory,
                  local_state_server=local_state_server)

    dump_euca_console(host_id, directory)
    retain_jenv(jenv_path, directory)


def retain_jenv(jenv_path, log_directory):
    if not jenv_path:
        return False

    try:
        shutil.copy(jenv_path, log_directory)
        return True
    except IOError:
        print_now("Failed to copy jenv file. Source: %s Destination: %s" %
                  (jenv_path, log_directory))
    return False


def get_machines_for_logs(client, bootstrap_host):
    # Try to get machine details from environment if possible.
    machine_addrs = dict(get_machine_addrs(client))

    # The bootstrap host always overrides the status output if
    # provided.
    if bootstrap_host:
        machine_addrs['0'] = bootstrap_host
    return machine_addrs


def get_machine_addrs(client):
    try:
        status = client.get_status()
    except Exception as err:
        logging.warning("Failed to retrieve status for dumping logs: %s", err)
        return
    for machine_id, machine in status.iter_machines():
        hostname = machine.get('dns-name')
        if hostname:
            yield machine_id, hostname


def dump_logs(client, host, directory, local_state_server=False):
    try:
        if local_state_server:
            copy_local_logs(directory, client)
        else:
            copy_remote_logs(host, directory)
        subprocess.check_call(
            ['gzip', '-f'] +
            glob.glob(os.path.join(directory, '*.log')))
    except Exception as e:
        print_now("Failed to retrieve logs")
        print_now(str(e))


lxc_template_glob = '/var/lib/juju/containers/juju-*-lxc-template/*.log'


def copy_local_logs(directory, client):
    local = get_local_root(get_juju_home(), client.env)
    log_names = [os.path.join(local, 'cloud-init-output.log')]
    log_names.extend(glob.glob(os.path.join(local, 'log', '*.log')))
    log_names.extend(glob.glob(lxc_template_glob))

    subprocess.check_call(['sudo', 'chmod', 'go+r'] + log_names)
    subprocess.check_call(['cp'] + log_names + [directory])


def copy_remote_logs(host, directory):
    """Copy as many logs from the remote host as possible to the directory."""
    # This list of names must be in the order of creation to ensure they
    # are retrieved.
    log_names = [
        'cloud-init*.log',
        'juju/*.log',
    ]
    source = 'ubuntu@%s:/var/log/{%s}' % (host, ','.join(log_names))

    try:
        wait_for_port(host, 22, timeout=60)
    except PortTimeoutError:
        logging.warning("Could not dump logs because port 22 was closed.")
        return

    try:
        subprocess.check_call([
            'timeout', '5m', 'ssh',
            '-o', 'UserKnownHostsFile /dev/null',
            '-o', 'StrictHostKeyChecking no',
            'ubuntu@' + host,
            'sudo chmod go+r /var/log/juju/*',
        ])
    except subprocess.CalledProcessError as e:
        # The juju log dir is not created until after cloud-init succeeds.
        logging.warning("Could not change the permission of the juju logs:")
        logging.warning(e.output)

    try:
        subprocess.check_call([
            'timeout', '5m', 'scp', '-C',
            '-o', 'UserKnownHostsFile /dev/null',
            '-o', 'StrictHostKeyChecking no',
            source, directory,
        ])
    except subprocess.CalledProcessError as e:
        # The juju logs will not exist if cloud-init failed.
        logging.warning("Could not retrieve some or all logs:")
        logging.warning(e.output)


def dump_euca_console(host_id, directory):
    if host_id is None:
        return
    with open(os.path.join(directory, 'console.log'), 'w') as console_file:
        subprocess.Popen(['euca-get-console-output', host_id],
                         stdout=console_file)


def assess_juju_run(client):
    responses = client.get_juju_output('run', '--format', 'json', '--machine',
                                       '1,2', 'uname')
    responses = json.loads(responses)
    for machine in responses:
        if machine.get('ReturnCode', 0) != 0:
            raise ValueError('juju run on machine %s returned %d: %s' % (
                             machine.get('MachineId'),
                             machine.get('ReturnCode'),
                             machine.get('Stderr')))
    return responses


def assess_upgrade(old_client, juju_path):
    client = EnvJujuClient.by_version(old_client.env, juju_path,
                                      old_client.debug)
    upgrade_juju(client)
    if client.env.config['type'] == 'maas':
        timeout = 1200
    else:
        timeout = 600
    client.wait_for_version(client.get_matching_agent_version(), timeout)
    assess_juju_run(client)


def upgrade_juju(client):
    client.set_testing_tools_metadata_url()
    tools_metadata_url = client.get_env_option('tools-metadata-url')
    print(
        'The tools-metadata-url is %s' % tools_metadata_url)
    client.upgrade_juju()


def describe_instances(instances=None, running=False, job_name=None,
                       env=None):
    command = ['euca-describe-instances']
    if job_name is not None:
        command.extend(['--filter', 'tag:job_name=%s' % job_name])
    if running:
        command.extend(['--filter', 'instance-state-name=running'])
    if instances is not None:
        command.extend(instances)
    logging.info(' '.join(command))
    return parse_euca(subprocess.check_output(command, env=env))


def get_job_instances(job_name):
    description = describe_instances(job_name=job_name, running=True)
    return (machine_id for machine_id, name in description)


def add_path_args(parser):
    parser.add_argument('--new-juju-bin', default=None,
                        help='Dirctory containing the new Juju binary.')
    parser.add_argument('--run-startup', help='Run common-startup.sh.',
                        action='store_true', default=False)


def add_output_args(parser):
    parser.add_argument('--debug', action="store_true", default=False,
                        help='Use --debug juju logging.')
    parser.add_argument('--verbose', '-v', action="store_true", default=False,
                        help='Increase logging verbosity.')


def add_juju_args(parser):
    parser.add_argument('--agent-url', default=None,
                        help='URL to use for retrieving agent binaries.')
    parser.add_argument('--series',
                        help='Name of the Ubuntu series to use.')


def get_juju_path(args):
    if args.run_startup:
        env = dict(os.environ)
        env.update({
            'ENV': args.env,
        })
        scripts = os.path.dirname(os.path.abspath(sys.argv[0]))
        subprocess.check_call(
            ['bash', '{}/common-startup.sh'.format(scripts)], env=env)
        bin_path = subprocess.check_output(['find', 'extracted-bin', '-name',
                                            'juju']).rstrip('\n')
        juju_path = os.path.abspath(bin_path)
    elif args.new_juju_bin is None:
        raise Exception('Either --new-juju-bin or --run-startup must be'
                        ' supplied.')
    else:
        juju_path = os.path.join(args.new_juju_bin, 'juju')
    return juju_path


def get_log_level(args):
    log_level = logging.INFO
    if args.verbose:
        log_level = logging.DEBUG
    return log_level


def deploy_job_parse_args(argv=None):
    parser = ArgumentParser('deploy_job')
    parser.add_argument('env', help='Base Juju environment.')
    parser.add_argument('logs', help='log directory.')
    parser.add_argument('job_name', help='Name of the Jenkins job.')
    parser.add_argument('--upgrade', action="store_true", default=False,
                        help='Perform an upgrade test.')
    parser.add_argument('--bootstrap-host',
                        help='The host to use for bootstrap.')
    parser.add_argument('--machine', help='A machine to add or when used with '
                        'KVM based MaaS, a KVM image to start.',
                        action='append', default=[])
    parser.add_argument('--keep-env', action='store_true', default=False,
                        help='Keep the Juju environment after the test'
                        ' completes.')
    parser.add_argument(
        '--upload-tools', action='store_true', default=False,
        help='upload local version of tools before bootstrapping')
    add_juju_args(parser)
    add_output_args(parser)
    add_path_args(parser)
    return parser.parse_args(argv)


def deploy_job():
    args = deploy_job_parse_args()
    configure_logging(get_log_level(args))
    juju_path = get_juju_path(args)
    series = args.series
    if series is None:
        series = 'precise'
    charm_prefix = 'local:{}/'.format(series)
    return _deploy_job(args.job_name, args.env, args.upgrade,
                       charm_prefix, args.bootstrap_host, args.machine,
                       args.series, args.logs, args.debug, juju_path,
                       args.agent_url, args.keep_env, args.upload_tools)


def update_env(env, new_env_name, series=None, bootstrap_host=None,
               agent_url=None):
    # Rename to the new name.
    env.environment = new_env_name
    if series is not None:
        env.config['default-series'] = series
    if bootstrap_host is not None:
        env.config['bootstrap-host'] = bootstrap_host
    if agent_url is not None:
        env.config['tools-metadata-url'] = agent_url


@contextmanager
def boot_context(job_name, client, bootstrap_host, machines, series,
                 agent_url, log_dir, keep_env, upload_tools):
    created_machines = False
    bootstrap_id = None
    running_domains = dict()
    try:
        if client.env.config['type'] == 'manual' and bootstrap_host is None:
            instances = run_instances(3, job_name)
            created_machines = True
            bootstrap_host = instances[0][1]
            bootstrap_id = instances[0][0]
            machines.extend(i[1] for i in instances[1:])
        if client.env.config['type'] == 'maas':
            for machine in machines:
                name, URI = machine.split('@')
                # Record already running domains, so they can be left running,
                # when finished with the test.
                if verify_libvirt_domain(URI, name, LIBVIRT_DOMAIN_RUNNING):
                    running_domains = {machine: True}
                    logging.info("%s is already running" % name)
                else:
                    running_domains = {machine: False}
                    logging.info("Attempting to start %s at %s" % (name, URI))
                    status_msg = start_libvirt_domain(URI, name)
                    logging.info("%s" % status_msg)
            # No further handling of machines down the line is required.
            machines = []

        update_env(client.env, job_name, series=series,
                   bootstrap_host=bootstrap_host, agent_url=agent_url)
        try:
            host = bootstrap_host
            ssh_machines = [] + machines
            if host is not None:
                ssh_machines.append(host)
            for machine in ssh_machines:
                logging.info('Waiting for port 22 on %s' % machine)
                wait_for_port(machine, 22, timeout=120)
            juju_home = get_juju_home()
            jenv_path = get_jenv_path(juju_home, client.env.environment)
            ensure_deleted(jenv_path)
            try:
                with temp_bootstrap_env(juju_home, client) as temp_juju_home:
                    client.bootstrap(upload_tools, temp_juju_home)
            except:
                if host is not None:
                    dump_logs(client, host, log_dir, bootstrap_id)
                raise
            try:
                if host is None:
                    host = get_machine_dns_name(client, 0)
                if host is None:
                    raise Exception('Could not get machine 0 host')
                try:
                    yield
                except BaseException as e:
                    logging.exception(e)
                    if host is not None:
                        dump_env_logs(
                            client, host, log_dir, host_id=bootstrap_id,
                            jenv_path=jenv_path)
                    sys.exit(1)
            finally:
                safe_print_status(client)
                if not keep_env:
                    client.destroy_environment()
        finally:
            if created_machines and not keep_env:
                destroy_job_instances(job_name)
    finally:
        if client.env.config['type'] == 'maas' and not keep_env:
            logging.info("Waiting for destroy-environment to complete")
            sleep(90)
            for machine, running in running_domains.items():
                name, URI = machine.split('@')
                if running:
                    logging.warning("%s at %s was running when deploy_job "
                                    "started. Shutting it down to ensure a "
                                    "clean environment." % (name, URI))
                logging.info("Attempting to stop %s at %s" % (name, URI))
                status_msg = stop_libvirt_domain(URI, name)
                logging.info("%s" % status_msg)


def _deploy_job(job_name, base_env, upgrade, charm_prefix, bootstrap_host,
                machines, series, log_dir, debug, juju_path, agent_url,
                keep_env, upload_tools):
    start_juju_path = None if upgrade else juju_path
    if sys.platform == 'win32':
        # Ensure OpenSSH is never in the path for win tests.
        sys.path = [p for p in sys.path if 'OpenSSH' not in p]
    client = EnvJujuClient.by_version(
        SimpleEnvironment.from_config(base_env), start_juju_path, debug)
    with boot_context(job_name, client, bootstrap_host, machines,
                      series, agent_url, log_dir, keep_env, upload_tools):
        prepare_environment(
            client, already_bootstrapped=True, machines=machines)
        if sys.platform in ('win32', 'darwin'):
            # The win and osx client tests only verify the client
            # can bootstrap and call the state-server.
            return
        client.juju('status', ())
        deploy_dummy_stack(client, charm_prefix)
        assess_juju_run(client)
        if upgrade:
            client.juju('status', ())
            assess_upgrade(client, juju_path)


def run_deployer():
    parser = ArgumentParser('Test with deployer')
    parser.add_argument('bundle_path',
                        help='URL or path to a bundle')
    parser.add_argument('env',
                        help='The juju environment to test')
    parser.add_argument('logs', help='log directory.')
    parser.add_argument('job_name', help='Name of the Jenkins job.')
    add_juju_args(parser)
    add_output_args(parser)
    add_path_args(parser)
    args = parser.parse_args()
    juju_path = get_juju_path(args)
    configure_logging(get_log_level(args))
    env = SimpleEnvironment.from_config(args.env)
    update_env(env, args.job_name, series=args.series,
               agent_url=args.agent_url)
    client = EnvJujuClient.by_version(env, juju_path, debug=args.debug)
    juju_home = get_juju_home()
    with temp_bootstrap_env(
            get_juju_home(), client, set_home=False) as juju_home:
        client.bootstrap(juju_home=juju_home)
    host = get_machine_dns_name(client, 0)
    if host is None:
        raise Exception('Could not get machine 0 host')
    try:
        client.deployer(args.bundle_path)
    except BaseException as e:
        logging.exception(e)
        sys.exit(1)
    finally:
        safe_print_status(client)
        client.destroy_environment()


def safe_print_status(client):
    """Show the output of juju status without raising exceptions."""
    try:
        client.juju('status', ())
    except Exception as e:
        logging.exception(e)


def get_machine_dns_name(client, machine):
    timeout = 600
    for remaining in until_timeout(timeout):
        bootstrap = client.get_status(
            timeout=timeout).status['machines'][str(machine)]
        host = bootstrap.get('dns-name')
        if host is not None and not host.startswith('172.'):
            return host


def wait_for_state_server_to_shutdown(host, client, instance_id):
    print_now("Waiting for port to close on %s" % host)
    wait_for_port(host, 17070, closed=True)
    print_now("Closed.")
    provider_type = client.env.config.get('type')
    if provider_type == 'openstack':
        environ = dict(os.environ)
        environ.update(translate_to_env(client.env.config))
        for ignored in until_timeout(300):
            output = subprocess.check_output(['nova', 'list'], env=environ)
            if instance_id not in output:
                print_now('{} was removed from nova list'.format(instance_id))
                break
        else:
            raise Exception(
                '{} was not deleted:\n{}'.format(instance_id, output))


def main():
    parser = ArgumentParser('Deploy a WordPress stack')
    parser.add_argument('--charm-prefix', help='A prefix for charm urls.',
                        default='')
    parser.add_argument('--already-bootstrapped',
                        help='The environment is already bootstrapped.',
                        action='store_true')
    parser.add_argument('--machine', help='A machine to add or when used with '
                        'KVM based MaaS, a KVM image to start.',
                        action='append', default=[])
    parser.add_argument('--dummy', help='Use dummy charms.',
                        action='store_true')
    parser.add_argument('env', help='The environment to deploy on.')
    args = parser.parse_args()
    try:
        client = EnvJujuClient.by_version(
            SimpleEnvironment.from_config(args.env))
        prepare_environment(client, args.already_bootstrapped, args.machine)
        if sys.platform in ('win32', 'darwin'):
            # The win and osx client tests only verify the client to
            # the state-server.
            return
        deploy_dummy_stack(client, args.charm_prefix)
    except Exception as e:
        print('%s (%s)' % (e, type(e).__name__))
        sys.exit(1)


if __name__ == '__main__':
    main()
