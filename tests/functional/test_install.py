import distutils
import glob
import os
import re
import shutil
import ssl
import sys
import textwrap
from os.path import curdir, join, pardir

import pytest
from pip._vendor.six import PY2

from pip import __version__ as pip_current_version
from pip._internal import pep425tags
from pip._internal.cli.status_codes import ERROR, SUCCESS
from pip._internal.models.index import PyPI, TestPyPI
from pip._internal.utils.misc import rmtree
from tests.lib import (
    _create_svn_repo,
    _create_test_package,
    create_basic_wheel_for_package,
    create_test_package_with_setup,
    need_bzr,
    need_mercurial,
    need_svn,
    path_to_url,
    pyversion,
    pyversion_tuple,
    requirements_file,
)
from tests.lib.filesystem import make_socket_file
from tests.lib.local_repos import local_checkout
from tests.lib.path import Path
from tests.lib.server import (
    file_response,
    make_mock_server,
    package_page,
    server_running,
)

skip_if_python2 = pytest.mark.skipif(PY2, reason="Non-Python 2 only")
skip_if_not_python2 = pytest.mark.skipif(not PY2, reason="Python 2 only")


@pytest.mark.parametrize('command', ('install', 'wheel'))
@pytest.mark.parametrize('variant', ('missing_setuptools', 'bad_setuptools'))
def test_pep518_uses_build_env(script, data, common_wheels, command, variant):
    if variant == 'missing_setuptools':
        script.pip("uninstall", "-y", "setuptools")
    elif variant == 'bad_setuptools':
        setuptools_mod = script.site_packages_path.joinpath("setuptools.py")
        with open(setuptools_mod, 'a') as f:
            f.write('\nraise ImportError("toto")')
    else:
        raise ValueError(variant)
    script.pip(
        command, '--no-index', '-f', common_wheels, '-f', data.packages,
        data.src.joinpath("pep518-3.0"),
    )


def test_pep518_build_env_uses_same_pip(
        script, data, pip_src, common_wheels, deprecated_python):
    """Ensure the subprocess call to pip for installing the
    build dependencies is using the same version of pip.
    """
    with open(script.scratch_path / 'pip.py', 'w') as fp:
        fp.write('raise ImportError')
    script.run(
        'python', pip_src / 'src/pip', 'install', '--no-index',
        '-f', common_wheels, '-f', data.packages,
        data.src.joinpath("pep518-3.0"),
        expect_stderr=deprecated_python,
    )


def test_pep518_refuses_conflicting_requires(script, data):
    create_basic_wheel_for_package(script, 'setuptools', '1.0')
    create_basic_wheel_for_package(script, 'wheel', '1.0')
    project_dir = data.src.joinpath("pep518_conflicting_requires")
    result = script.pip_install_local('-f', script.scratch_path,
                                      project_dir, expect_error=True)
    assert (
        result.returncode != 0 and
        ('Some build dependencies for %s conflict with PEP 517/518 supported '
         'requirements: setuptools==1.0 is incompatible with '
         'setuptools>=40.8.0.' % path_to_url(project_dir)) in result.stderr
    ), str(result)


def test_pep518_refuses_invalid_requires(script, data, common_wheels):
    result = script.pip(
        'install', '-f', common_wheels,
        data.src.joinpath("pep518_invalid_requires"),
        expect_error=True
    )
    assert result.returncode == 1
    assert "does not comply with PEP 518" in result.stderr


def test_pep518_refuses_invalid_build_system(script, data, common_wheels):
    result = script.pip(
        'install', '-f', common_wheels,
        data.src.joinpath("pep518_invalid_build_system"),
        expect_error=True
    )
    assert result.returncode == 1
    assert "does not comply with PEP 518" in result.stderr


def test_pep518_allows_missing_requires(script, data, common_wheels):
    result = script.pip(
        'install', '-f', common_wheels,
        data.src.joinpath("pep518_missing_requires"),
        expect_stderr=True
    )
    # Make sure we don't warn when this occurs.
    assert "does not comply with PEP 518" not in result.stderr

    # We want it to go through isolation for now.
    assert "Installing build dependencies" in result.stdout, result.stdout

    assert result.returncode == 0
    assert result.files_created


@pytest.mark.incompatible_with_test_venv
def test_pep518_with_user_pip(script, pip_src, data, common_wheels):
    """
    Check that build dependencies are installed into the build
    environment without using build isolation for the pip invocation.

    To ensure that we're not using build isolation when installing
    the build dependencies, we install a user copy of pip in the
    non-isolated environment, and break pip in the system site-packages,
    so that isolated uses of pip will fail.
    """
    script.pip("install", "--ignore-installed",
               "-f", common_wheels, "--user", pip_src)
    system_pip_dir = script.site_packages_path / 'pip'
    assert not system_pip_dir.exists()
    system_pip_dir.mkdir()
    with open(system_pip_dir / '__init__.py', 'w') as fp:
        fp.write('raise ImportError\n')
    script.pip(
        'wheel', '--no-index', '-f', common_wheels, '-f', data.packages,
        data.src.joinpath("pep518-3.0"),
    )


def test_pep518_with_extra_and_markers(script, data, common_wheels):
    script.pip(
        'wheel', '--no-index',
        '-f', common_wheels,
        '-f', data.find_links,
        data.src.joinpath("pep518_with_extra_and_markers-1.0"),
    )


def test_pep518_with_namespace_package(script, data, common_wheels):
    script.pip(
        'wheel', '--no-index',
        '-f', common_wheels,
        '-f', data.find_links,
        data.src.joinpath("pep518_with_namespace_package-1.0"),
        use_module=True,
    )


@pytest.mark.timeout(60)
@pytest.mark.parametrize('command', ('install', 'wheel'))
@pytest.mark.parametrize('package', ('pep518_forkbomb',
                                     'pep518_twin_forkbombs_first',
                                     'pep518_twin_forkbombs_second'))
def test_pep518_forkbombs(script, data, common_wheels, command, package):
    package_source = next(data.packages.glob(package + '-[0-9]*.tar.gz'))
    result = script.pip(
        command, '--no-index', '-v',
        '-f', common_wheels,
        '-f', data.find_links,
        package,
        expect_error=True,
    )
    assert '{1} is already being built: {0} from {1}'.format(
        package, path_to_url(package_source),
    ) in result.stderr, str(result)


@pytest.mark.network
def test_pip_second_command_line_interface_works(
        script, pip_src, data, common_wheels, deprecated_python):
    """
    Check if ``pip<PYVERSION>`` commands behaves equally
    """
    # Re-install pip so we get the launchers.
    script.pip_install_local('-f', common_wheels, pip_src)
    # On old versions of Python, urllib3/requests will raise a warning about
    # the lack of an SSLContext.
    kwargs = {'expect_stderr': deprecated_python}
    if pyversion_tuple < (2, 7, 9):
        kwargs['expect_stderr'] = True

    args = ['pip%s' % pyversion]
    args.extend(['install', 'INITools==0.2'])
    args.extend(['-f', data.packages])
    result = script.run(*args, **kwargs)
    egg_info_folder = (
        script.site_packages / 'INITools-0.2-py%s.egg-info' % pyversion
    )
    initools_folder = script.site_packages / 'initools'
    assert egg_info_folder in result.files_created, str(result)
    assert initools_folder in result.files_created, str(result)


def test_install_exit_status_code_when_no_requirements(script):
    """
    Test install exit status code when no requirements specified
    """
    result = script.pip('install', expect_error=True)
    assert "You must give at least one requirement to install" in result.stderr
    assert result.returncode == ERROR


def test_install_exit_status_code_when_blank_requirements_file(script):
    """
    Test install exit status code when blank requirements file specified
    """
    script.scratch_path.joinpath("blank.txt").write_text("\n")
    script.pip('install', '-r', 'blank.txt')


@pytest.mark.network
def test_basic_install_from_pypi(script):
    """
    Test installing a package from PyPI.
    """
    result = script.pip('install', 'INITools==0.2')
    egg_info_folder = (
        script.site_packages / 'INITools-0.2-py%s.egg-info' % pyversion
    )
    initools_folder = script.site_packages / 'initools'
    assert egg_info_folder in result.files_created, str(result)
    assert initools_folder in result.files_created, str(result)

    # Should not display where it's looking for files
    assert "Looking in indexes: " not in result.stdout
    assert "Looking in links: " not in result.stdout

    # Ensure that we don't print the full URL.
    #    The URL should be trimmed to only the last part of the path in it,
    #    when installing from PyPI. The assertion here only checks for
    #    `https://` since that's likely to show up if we're not trimming in
    #    the correct circumstances.
    assert "https://" not in result.stdout


def test_basic_editable_install(script):
    """
    Test editable installation.
    """
    result = script.pip('install', '-e', 'INITools==0.2', expect_error=True)
    assert (
        "INITools==0.2 is not a valid editable requirement"
        in result.stderr
    )
    assert not result.files_created


@need_svn
def test_basic_install_editable_from_svn(script):
    """
    Test checking out from svn.
    """
    checkout_path = _create_test_package(script)
    repo_url = _create_svn_repo(script, checkout_path)
    result = script.pip(
        'install',
        '-e', 'svn+' + repo_url + '#egg=version-pkg'
    )
    result.assert_installed('version-pkg', with_files=['.svn'])


def _test_install_editable_from_git(script, tmpdir):
    """Test cloning from Git."""
    pkg_path = _create_test_package(script, name='testpackage', vcs='git')
    args = ['install', '-e', 'git+%s#egg=testpackage' % path_to_url(pkg_path)]
    result = script.pip(*args)
    result.assert_installed('testpackage', with_files=['.git'])


def test_basic_install_editable_from_git(script, tmpdir):
    _test_install_editable_from_git(script, tmpdir)


def test_install_editable_from_git_autobuild_wheel(
        script, tmpdir, with_wheel):
    _test_install_editable_from_git(script, tmpdir)


@pytest.mark.network
def test_install_editable_uninstalls_existing(data, script, tmpdir):
    """
    Test that installing an editable uninstalls a previously installed
    non-editable version.
    https://github.com/pypa/pip/issues/1548
    https://github.com/pypa/pip/pull/1552
    """
    to_install = data.packages.joinpath("pip-test-package-0.1.tar.gz")
    result = script.pip_install_local(to_install)
    assert 'Successfully installed pip-test-package' in result.stdout
    result.assert_installed('piptestpackage', editable=False)

    result = script.pip(
        'install', '-e',
        '%s#egg=pip-test-package' %
        local_checkout(
            'git+https://github.com/pypa/pip-test-package.git', tmpdir,
        ),
    )
    result.assert_installed('pip-test-package', with_files=['.git'])
    assert 'Found existing installation: pip-test-package 0.1' in result.stdout
    assert 'Uninstalling pip-test-package-' in result.stdout
    assert 'Successfully uninstalled pip-test-package' in result.stdout


def test_install_editable_uninstalls_existing_from_path(script, data):
    """
    Test that installing an editable uninstalls a previously installed
    non-editable version from path
    """
    to_install = data.src.joinpath('simplewheel-1.0')
    result = script.pip_install_local(to_install)
    assert 'Successfully installed simplewheel' in result.stdout
    simple_folder = script.site_packages / 'simplewheel'
    result.assert_installed('simplewheel', editable=False)
    assert simple_folder in result.files_created, str(result.stdout)

    result = script.pip(
        'install', '-e',
        to_install,
    )
    install_path = script.site_packages / 'simplewheel.egg-link'
    assert install_path in result.files_created, str(result)
    assert 'Found existing installation: simplewheel 1.0' in result.stdout
    assert 'Uninstalling simplewheel-' in result.stdout
    assert 'Successfully uninstalled simplewheel' in result.stdout
    assert simple_folder in result.files_deleted, str(result.stdout)


@need_mercurial
def test_basic_install_editable_from_hg(script, tmpdir):
    """Test cloning and hg+file install from Mercurial."""
    pkg_path = _create_test_package(script, name='testpackage', vcs='hg')
    url = 'hg+{}#egg=testpackage'.format(path_to_url(pkg_path))
    assert url.startswith('hg+file')
    args = ['install', '-e', url]
    result = script.pip(*args)
    result.assert_installed('testpackage', with_files=['.hg'])


@need_mercurial
def test_vcs_url_final_slash_normalization(script, tmpdir):
    """
    Test that presence or absence of final slash in VCS URL is normalized.
    """
    pkg_path = _create_test_package(script, name='testpackage', vcs='hg')
    args = ['install', '-e', 'hg+%s/#egg=testpackage' % path_to_url(pkg_path)]
    result = script.pip(*args)
    result.assert_installed('testpackage', with_files=['.hg'])


@need_bzr
def test_install_editable_from_bazaar(script, tmpdir):
    """Test checking out from Bazaar."""
    pkg_path = _create_test_package(script, name='testpackage', vcs='bazaar')
    args = ['install', '-e', 'bzr+%s/#egg=testpackage' % path_to_url(pkg_path)]
    result = script.pip(*args)
    result.assert_installed('testpackage', with_files=['.bzr'])


@pytest.mark.network
@need_bzr
def test_vcs_url_urlquote_normalization(script, tmpdir):
    """
    Test that urlquoted characters are normalized for repo URL comparison.
    """
    script.pip(
        'install', '-e',
        '%s/#egg=django-wikiapp' %
        local_checkout(
            'bzr+http://bazaar.launchpad.net/%7Edjango-wikiapp/django-wikiapp'
            '/release-0.1',
            tmpdir,
        ),
    )


def test_basic_install_from_local_directory(script, data):
    """
    Test installing from a local directory.
    """
    to_install = data.packages.joinpath("FSPkg")
    result = script.pip('install', to_install, expect_error=False)
    fspkg_folder = script.site_packages / 'fspkg'
    egg_info_folder = (
        script.site_packages / 'FSPkg-0.1.dev0-py%s.egg-info' % pyversion
    )
    assert fspkg_folder in result.files_created, str(result.stdout)
    assert egg_info_folder in result.files_created, str(result)


def test_basic_install_relative_directory(script, data):
    """
    Test installing a requirement using a relative path.
    """
    egg_info_file = (
        script.site_packages / 'FSPkg-0.1.dev0-py%s.egg-info' % pyversion
    )
    egg_link_file = (
        script.site_packages / 'FSPkg.egg-link'
    )
    package_folder = script.site_packages / 'fspkg'

    # Compute relative install path to FSPkg from scratch path.
    full_rel_path = Path(
        os.path.relpath(data.packages.joinpath('FSPkg'), script.scratch_path)
    )
    full_rel_url = (
        'file:' + full_rel_path.replace(os.path.sep, '/') + '#egg=FSPkg'
    )
    embedded_rel_path = script.scratch_path.joinpath(full_rel_path)

    # For each relative path, install as either editable or not using either
    # URLs with egg links or not.
    for req_path in (full_rel_path, full_rel_url, embedded_rel_path):
        # Regular install.
        result = script.pip('install', req_path,
                            cwd=script.scratch_path)
        assert egg_info_file in result.files_created, str(result)
        assert package_folder in result.files_created, str(result)
        script.pip('uninstall', '-y', 'fspkg')

        # Editable install.
        result = script.pip('install', '-e' + req_path,
                            cwd=script.scratch_path)
        assert egg_link_file in result.files_created, str(result)
        script.pip('uninstall', '-y', 'fspkg')


def test_install_quiet(script, data):
    """
    Test that install -q is actually quiet.
    """
    # Apparently if pip install -q is not actually quiet, then it breaks
    # everything. See:
    #   https://github.com/pypa/pip/issues/3418
    #   https://github.com/docker-library/python/issues/83
    to_install = data.packages.joinpath("FSPkg")
    result = script.pip('install', '-qqq', to_install, expect_error=False)
    assert result.stdout == ""
    assert result.stderr == ""


def test_hashed_install_success(script, data, tmpdir):
    """
    Test that installing various sorts of requirements with correct hashes
    works.

    Test file URLs and index packages (which become HTTP URLs behind the
    scenes).

    """
    file_url = path_to_url(
        (data.packages / 'simple-1.0.tar.gz').resolve())
    with requirements_file(
            'simple2==1.0 --hash=sha256:9336af72ca661e6336eb87bc7de3e8844d853e'
            '3848c2b9bbd2e8bf01db88c2c7\n'
            '{simple} --hash=sha256:393043e672415891885c9a2a0929b1af95fb866d6c'
            'a016b42d2e6ce53619b653'.format(simple=file_url),
            tmpdir) as reqs_file:
        script.pip_install_local('-r', reqs_file.resolve(), expect_error=False)


def test_hashed_install_failure(script, tmpdir):
    """Test that wrong hashes stop installation.

    This makes sure prepare_files() is called in the course of installation
    and so has the opportunity to halt if hashes are wrong. Checks on various
    kinds of hashes are in test_req.py.

    """
    with requirements_file('simple2==1.0 --hash=sha256:9336af72ca661e6336eb87b'
                           'c7de3e8844d853e3848c2b9bbd2e8bf01db88c2c\n',
                           tmpdir) as reqs_file:
        result = script.pip_install_local('-r',
                                          reqs_file.resolve(),
                                          expect_error=True)
    assert len(result.files_created) == 0


def assert_re_match(pattern, text):
    assert re.search(pattern, text), (
        "Could not find {!r} in {!r}".format(pattern, text)
    )


@pytest.mark.network
def test_hashed_install_failure_later_flag(script, tmpdir):
    with requirements_file(
        "blessings==1.0\n"
        "tracefront==0.1 --hash=sha256:somehash\n"
        "https://files.pythonhosted.org/packages/source/m/more-itertools/"
        "more-itertools-1.0.tar.gz#md5=b21850c3cfa7efbb70fd662ab5413bdd\n"
        "https://files.pythonhosted.org/"
        "packages/source/p/peep/peep-3.1.1.tar.gz\n",
        tmpdir,
    ) as reqs_file:
        result = script.pip(
            "install", "-r", reqs_file.resolve(), expect_error=True
        )

    assert_re_match(
        r'Hashes are required in --require-hashes mode, but they are '
        r'missing .*\n'
        r'    https://files\.pythonhosted\.org/packages/source/p/peep/peep'
        r'-3\.1\.1\.tar\.gz --hash=sha256:[0-9a-f]+\n'
        r'    blessings==1.0 --hash=sha256:[0-9a-f]+\n'
        r'THESE PACKAGES DO NOT MATCH THE HASHES.*\n'
        r'    tracefront==0.1 .*:\n'
        r'        Expected sha256 somehash\n'
        r'             Got        [0-9a-f]+',
        result.stderr,
    )


def test_install_from_local_directory_with_symlinks_to_directories(
        script, data):
    """
    Test installing from a local directory containing symlinks to directories.
    """
    to_install = data.packages.joinpath("symlinks")
    result = script.pip('install', to_install, expect_error=False)
    pkg_folder = script.site_packages / 'symlinks'
    egg_info_folder = (
        script.site_packages / 'symlinks-0.1.dev0-py%s.egg-info' % pyversion
    )
    assert pkg_folder in result.files_created, str(result.stdout)
    assert egg_info_folder in result.files_created, str(result)


@pytest.mark.skipif("sys.platform == 'win32' or sys.version_info < (3,)")
def test_install_from_local_directory_with_socket_file(script, data, tmpdir):
    """
    Test installing from a local directory containing a socket file.
    """
    egg_info_file = (
        script.site_packages / "FSPkg-0.1.dev0-py%s.egg-info" % pyversion
    )
    package_folder = script.site_packages / "fspkg"
    to_copy = data.packages.joinpath("FSPkg")
    to_install = tmpdir.joinpath("src")

    shutil.copytree(to_copy, to_install)
    # Socket file, should be ignored.
    socket_file_path = os.path.join(to_install, "example")
    make_socket_file(socket_file_path)

    result = script.pip("install", "--verbose", to_install, expect_error=False)
    assert package_folder in result.files_created, str(result.stdout)
    assert egg_info_file in result.files_created, str(result)
    assert str(socket_file_path) in result.stderr


def test_install_from_local_directory_with_no_setup_py(script, data):
    """
    Test installing from a local directory with no 'setup.py'.
    """
    result = script.pip('install', data.root, expect_error=True)
    assert not result.files_created
    assert "is not installable." in result.stderr
    assert "Neither 'setup.py' nor 'pyproject.toml' found." in result.stderr


def test_editable_install__local_dir_no_setup_py(
        script, data, deprecated_python):
    """
    Test installing in editable mode from a local directory with no setup.py.
    """
    result = script.pip('install', '-e', data.root, expect_error=True)
    assert not result.files_created

    msg = result.stderr
    if deprecated_python:
        assert 'File "setup.py" not found. ' in msg
    else:
        assert msg.startswith('ERROR: File "setup.py" not found. ')
    assert 'pyproject.toml' not in msg


def test_editable_install__local_dir_no_setup_py_with_pyproject(
        script, deprecated_python):
    """
    Test installing in editable mode from a local directory with no setup.py
    but that does have pyproject.toml.
    """
    local_dir = script.scratch_path.joinpath('temp')
    local_dir.mkdir()
    pyproject_path = local_dir.joinpath('pyproject.toml')
    pyproject_path.write_text('')

    result = script.pip('install', '-e', local_dir, expect_error=True)
    assert not result.files_created

    msg = result.stderr
    if deprecated_python:
        assert 'File "setup.py" not found. ' in msg
    else:
        assert msg.startswith('ERROR: File "setup.py" not found. ')
    assert 'A "pyproject.toml" file was found' in msg


@skip_if_not_python2
@pytest.mark.xfail
def test_install_argparse_shadowed(script):
    # When argparse is in the stdlib, we support installing it
    # even though that's pretty useless because older packages did need to
    # depend on it, and not having its metadata will cause pkg_resources
    # requirements checks to fail // trigger easy-install, both of which are
    # bad.
    # XXX: Note, this test hits the outside-environment check, not the
    # in-stdlib check, because our tests run in virtualenvs...
    result = script.pip('install', 'argparse>=1.4')
    assert "Not uninstalling argparse" in result.stdout


@pytest.mark.network
@skip_if_python2
def test_upgrade_argparse_shadowed(script):
    # If argparse is installed - even if shadowed for imported - we support
    # upgrading it and properly remove the older versions files.
    script.pip('install', 'argparse==1.3')
    result = script.pip('install', 'argparse>=1.4')
    assert "Not uninstalling argparse" not in result.stdout


def test_install_curdir(script, data):
    """
    Test installing current directory ('.').
    """
    run_from = data.packages.joinpath("FSPkg")
    # Python 2.4 Windows balks if this exists already
    egg_info = join(run_from, "FSPkg.egg-info")
    if os.path.isdir(egg_info):
        rmtree(egg_info)
    result = script.pip('install', curdir, cwd=run_from, expect_error=False)
    fspkg_folder = script.site_packages / 'fspkg'
    egg_info_folder = (
        script.site_packages / 'FSPkg-0.1.dev0-py%s.egg-info' % pyversion
    )
    assert fspkg_folder in result.files_created, str(result.stdout)
    assert egg_info_folder in result.files_created, str(result)


def test_install_pardir(script, data):
    """
    Test installing parent directory ('..').
    """
    run_from = data.packages.joinpath("FSPkg", "fspkg")
    result = script.pip('install', pardir, cwd=run_from, expect_error=False)
    fspkg_folder = script.site_packages / 'fspkg'
    egg_info_folder = (
        script.site_packages / 'FSPkg-0.1.dev0-py%s.egg-info' % pyversion
    )
    assert fspkg_folder in result.files_created, str(result.stdout)
    assert egg_info_folder in result.files_created, str(result)


@pytest.mark.network
def test_install_global_option(script):
    """
    Test using global distutils options.
    (In particular those that disable the actual install action)
    """
    result = script.pip(
        'install', '--global-option=--version', "INITools==0.1",
        expect_stderr=True)
    assert 'INITools==0.1\n' in result.stdout
    assert not result.files_created


def test_install_with_hacked_egg_info(script, data):
    """
    test installing a package which defines its own egg_info class
    """
    run_from = data.packages.joinpath("HackedEggInfo")
    result = script.pip('install', '.', cwd=run_from)
    assert 'Successfully installed hackedegginfo-0.0.0\n' in result.stdout


@pytest.mark.network
def test_install_using_install_option_and_editable(script, tmpdir):
    """
    Test installing a tool using -e and --install-option
    """
    folder = 'script_folder'
    script.scratch_path.joinpath(folder).mkdir()
    url = 'git+git://github.com/pypa/pip-test-package'
    result = script.pip(
        'install', '-e', '%s#egg=pip-test-package' %
        local_checkout(url, tmpdir),
        '--install-option=--script-dir=%s' % folder,
        expect_stderr=True)
    script_file = (
        script.venv / 'src' / 'pip-test-package' /
        folder / 'pip-test-package' + script.exe
    )
    assert script_file in result.files_created


@pytest.mark.network
@need_mercurial
def test_install_global_option_using_editable(script, tmpdir):
    """
    Test using global distutils options, but in an editable installation
    """
    url = 'hg+http://bitbucket.org/runeh/anyjson'
    result = script.pip(
        'install', '--global-option=--version', '-e',
        '%s@0.2.5#egg=anyjson' % local_checkout(url, tmpdir),
        expect_stderr=True)
    assert 'Successfully installed anyjson' in result.stdout


@pytest.mark.network
def test_install_package_with_same_name_in_curdir(script):
    """
    Test installing a package with the same name of a local folder
    """
    script.scratch_path.joinpath("mock==0.6").mkdir()
    result = script.pip('install', 'mock==0.6')
    egg_folder = script.site_packages / 'mock-0.6.0-py%s.egg-info' % pyversion
    assert egg_folder in result.files_created, str(result)


mock100_setup_py = textwrap.dedent('''\
                        from setuptools import setup
                        setup(name='mock',
                              version='100.1')''')


def test_install_folder_using_dot_slash(script):
    """
    Test installing a folder using pip install ./foldername
    """
    script.scratch_path.joinpath("mock").mkdir()
    pkg_path = script.scratch_path / 'mock'
    pkg_path.joinpath("setup.py").write_text(mock100_setup_py)
    result = script.pip('install', './mock')
    egg_folder = script.site_packages / 'mock-100.1-py%s.egg-info' % pyversion
    assert egg_folder in result.files_created, str(result)


def test_install_folder_using_slash_in_the_end(script):
    r"""
    Test installing a folder using pip install foldername/ or foldername\
    """
    script.scratch_path.joinpath("mock").mkdir()
    pkg_path = script.scratch_path / 'mock'
    pkg_path.joinpath("setup.py").write_text(mock100_setup_py)
    result = script.pip('install', 'mock' + os.path.sep)
    egg_folder = script.site_packages / 'mock-100.1-py%s.egg-info' % pyversion
    assert egg_folder in result.files_created, str(result)


def test_install_folder_using_relative_path(script):
    """
    Test installing a folder using pip install folder1/folder2
    """
    script.scratch_path.joinpath("initools").mkdir()
    script.scratch_path.joinpath("initools", "mock").mkdir()
    pkg_path = script.scratch_path / 'initools' / 'mock'
    pkg_path.joinpath("setup.py").write_text(mock100_setup_py)
    result = script.pip('install', Path('initools') / 'mock')
    egg_folder = script.site_packages / 'mock-100.1-py%s.egg-info' % pyversion
    assert egg_folder in result.files_created, str(result)


@pytest.mark.network
def test_install_package_which_contains_dev_in_name(script):
    """
    Test installing package from PyPI which contains 'dev' in name
    """
    result = script.pip('install', 'django-devserver==0.0.4')
    devserver_folder = script.site_packages / 'devserver'
    egg_info_folder = (
        script.site_packages / 'django_devserver-0.0.4-py%s.egg-info' %
        pyversion
    )
    assert devserver_folder in result.files_created, str(result.stdout)
    assert egg_info_folder in result.files_created, str(result)


def test_install_package_with_target(script):
    """
    Test installing a package using pip install --target
    """
    target_dir = script.scratch_path / 'target'
    result = script.pip_install_local('-t', target_dir, "simple==1.0")
    assert Path('scratch') / 'target' / 'simple' in result.files_created, (
        str(result)
    )

    # Test repeated call without --upgrade, no files should have changed
    result = script.pip_install_local(
        '-t', target_dir, "simple==1.0", expect_stderr=True,
    )
    assert not Path('scratch') / 'target' / 'simple' in result.files_updated

    # Test upgrade call, check that new version is installed
    result = script.pip_install_local('--upgrade', '-t',
                                      target_dir, "simple==2.0")
    assert Path('scratch') / 'target' / 'simple' in result.files_updated, (
        str(result)
    )
    egg_folder = (
        Path('scratch') / 'target' / 'simple-2.0-py%s.egg-info' % pyversion)
    assert egg_folder in result.files_created, (
        str(result)
    )

    # Test install and upgrade of single-module package
    result = script.pip_install_local('-t', target_dir, 'singlemodule==0.0.0')
    singlemodule_py = Path('scratch') / 'target' / 'singlemodule.py'
    assert singlemodule_py in result.files_created, str(result)

    result = script.pip_install_local('-t', target_dir, 'singlemodule==0.0.1',
                                      '--upgrade')
    assert singlemodule_py in result.files_updated, str(result)


def test_install_nonlocal_compatible_wheel(script, data):
    target_dir = script.scratch_path / 'target'

    # Test install with --target
    result = script.pip(
        'install',
        '-t', target_dir,
        '--no-index', '--find-links', data.find_links,
        '--only-binary=:all:',
        '--python', '3',
        '--platform', 'fakeplat',
        '--abi', 'fakeabi',
        'simplewheel',
    )
    assert result.returncode == SUCCESS

    distinfo = Path('scratch') / 'target' / 'simplewheel-2.0-1.dist-info'
    assert distinfo in result.files_created

    # Test install without --target
    result = script.pip(
        'install',
        '--no-index', '--find-links', data.find_links,
        '--only-binary=:all:',
        '--python', '3',
        '--platform', 'fakeplat',
        '--abi', 'fakeabi',
        'simplewheel',
        expect_error=True
    )
    assert result.returncode == ERROR


def test_install_nonlocal_compatible_wheel_path(script, data):
    target_dir = script.scratch_path / 'target'

    # Test a full path requirement
    result = script.pip(
        'install',
        '-t', target_dir,
        '--no-index',
        '--only-binary=:all:',
        Path(data.packages) / 'simplewheel-2.0-py3-fakeabi-fakeplat.whl'
    )
    assert result.returncode == SUCCESS

    distinfo = Path('scratch') / 'target' / 'simplewheel-2.0.dist-info'
    assert distinfo in result.files_created

    # Test a full path requirement (without --target)
    result = script.pip(
        'install',
        '--no-index',
        '--only-binary=:all:',
        Path(data.packages) / 'simplewheel-2.0-py3-fakeabi-fakeplat.whl',
        expect_error=True
    )
    assert result.returncode == ERROR


def test_install_with_target_and_scripts_no_warning(script, with_wheel):
    """
    Test that installing with --target does not trigger the "script not
    in PATH" warning (issue #5201)
    """
    target_dir = script.scratch_path / 'target'
    pkga_path = script.scratch_path / 'pkga'
    pkga_path.mkdir()
    pkga_path.joinpath("setup.py").write_text(textwrap.dedent("""
        from setuptools import setup
        setup(name='pkga',
              version='0.1',
              py_modules=["pkga"],
              entry_points={
                  'console_scripts': ['pkga=pkga:main']
              }
        )
    """))
    pkga_path.joinpath("pkga.py").write_text(textwrap.dedent("""
        def main(): pass
    """))
    result = script.pip('install', '--target', target_dir, pkga_path)
    # This assertion isn't actually needed, if we get the script warning
    # the script.pip() call will fail with "stderr not expected". But we
    # leave the assertion to make the intention of the code clearer.
    assert "--no-warn-script-location" not in result.stderr, str(result)


def test_install_package_with_root(script, data):
    """
    Test installing a package using pip install --root
    """
    root_dir = script.scratch_path / 'root'
    result = script.pip(
        'install', '--root', root_dir, '-f', data.find_links, '--no-index',
        'simple==1.0',
    )
    normal_install_path = (
        script.base_path / script.site_packages / 'simple-1.0-py%s.egg-info' %
        pyversion
    )
    # use distutils to change the root exactly how the --root option does it
    from distutils.util import change_root
    root_path = change_root(
        os.path.join(script.scratch, 'root'),
        normal_install_path
    )
    assert root_path in result.files_created, str(result)

    # Should show find-links location in output
    assert "Looking in indexes: " not in result.stdout
    assert "Looking in links: " in result.stdout


def test_install_package_with_prefix(script, data):
    """
    Test installing a package using pip install --prefix
    """
    prefix_path = script.scratch_path / 'prefix'
    result = script.pip(
        'install', '--prefix', prefix_path, '-f', data.find_links,
        '--no-binary', 'simple', '--no-index', 'simple==1.0',
    )

    rel_prefix_path = script.scratch / 'prefix'
    install_path = (
        distutils.sysconfig.get_python_lib(prefix=rel_prefix_path) /
        'simple-1.0-py{}.egg-info'.format(pyversion)
    )
    assert install_path in result.files_created, str(result)


def test_install_editable_with_prefix(script):
    # make a dummy project
    pkga_path = script.scratch_path / 'pkga'
    pkga_path.mkdir()
    pkga_path.joinpath("setup.py").write_text(textwrap.dedent("""
        from setuptools import setup
        setup(name='pkga',
              version='0.1')
    """))

    if hasattr(sys, "pypy_version_info"):
        site_packages = os.path.join(
            'prefix', 'lib', 'python{}'.format(pyversion), 'site-packages')
    else:
        site_packages = distutils.sysconfig.get_python_lib(prefix='prefix')

    # make sure target path is in PYTHONPATH
    pythonpath = script.scratch_path / site_packages
    pythonpath.mkdir(parents=True)
    script.environ["PYTHONPATH"] = pythonpath

    # install pkga package into the absolute prefix directory
    prefix_path = script.scratch_path / 'prefix'
    result = script.pip(
        'install', '--editable', pkga_path, '--prefix', prefix_path)

    # assert pkga is installed at correct location
    install_path = script.scratch / site_packages / 'pkga.egg-link'
    assert install_path in result.files_created, str(result)


def test_install_package_conflict_prefix_and_user(script, data):
    """
    Test installing a package using pip install --prefix --user errors out
    """
    prefix_path = script.scratch_path / 'prefix'
    result = script.pip(
        'install', '-f', data.find_links, '--no-index', '--user',
        '--prefix', prefix_path, 'simple==1.0',
        expect_error=True, quiet=True,
    )
    assert (
        "Can not combine '--user' and '--prefix'" in result.stderr
    )


def test_install_package_that_emits_unicode(script, data):
    """
    Install a package with a setup.py that emits UTF-8 output and then fails.

    Refs https://github.com/pypa/pip/issues/326
    """
    to_install = data.packages.joinpath("BrokenEmitsUTF8")
    result = script.pip(
        'install', to_install, expect_error=True, expect_temp=True, quiet=True,
    )
    assert (
        'FakeError: this package designed to fail on install' in result.stderr
    ), 'stderr: {}'.format(result.stderr)
    assert 'UnicodeDecodeError' not in result.stderr
    assert 'UnicodeDecodeError' not in result.stdout


def test_install_package_with_utf8_setup(script, data):
    """Install a package with a setup.py that declares a utf-8 encoding."""
    to_install = data.packages.joinpath("SetupPyUTF8")
    script.pip('install', to_install)


def test_install_package_with_latin1_setup(script, data):
    """Install a package with a setup.py that declares a latin-1 encoding."""
    to_install = data.packages.joinpath("SetupPyLatin1")
    script.pip('install', to_install)


def test_url_req_case_mismatch_no_index(script, data):
    """
    tar ball url requirements (with no egg fragment), that happen to have upper
    case project names, should be considered equal to later requirements that
    reference the project name using lower case.

    tests/data/packages contains Upper-1.0.tar.gz and Upper-2.0.tar.gz
    'requiresupper' has install_requires = ['upper']
    """
    Upper = '/'.join((data.find_links, 'Upper-1.0.tar.gz'))
    result = script.pip(
        'install', '--no-index', '-f', data.find_links, Upper, 'requiresupper'
    )

    # only Upper-1.0.tar.gz should get installed.
    egg_folder = script.site_packages / 'Upper-1.0-py%s.egg-info' % pyversion
    assert egg_folder in result.files_created, str(result)
    egg_folder = script.site_packages / 'Upper-2.0-py%s.egg-info' % pyversion
    assert egg_folder not in result.files_created, str(result)


def test_url_req_case_mismatch_file_index(script, data):
    """
    tar ball url requirements (with no egg fragment), that happen to have upper
    case project names, should be considered equal to later requirements that
    reference the project name using lower case.

    tests/data/packages3 contains Dinner-1.0.tar.gz and Dinner-2.0.tar.gz
    'requiredinner' has install_requires = ['dinner']

    This test is similar to test_url_req_case_mismatch_no_index; that test
    tests behaviour when using "--no-index -f", while this one does the same
    test when using "--index-url". Unfortunately this requires a different
    set of packages as it requires a prepared index.html file and
    subdirectory-per-package structure.
    """
    Dinner = '/'.join((data.find_links3, 'dinner', 'Dinner-1.0.tar.gz'))
    result = script.pip(
        'install', '--index-url', data.find_links3, Dinner, 'requiredinner'
    )

    # only Upper-1.0.tar.gz should get installed.
    egg_folder = script.site_packages / 'Dinner-1.0-py%s.egg-info' % pyversion
    assert egg_folder in result.files_created, str(result)
    egg_folder = script.site_packages / 'Dinner-2.0-py%s.egg-info' % pyversion
    assert egg_folder not in result.files_created, str(result)


def test_url_incorrect_case_no_index(script, data):
    """
    Same as test_url_req_case_mismatch_no_index, except testing for the case
    where the incorrect case is given in the name of the package to install
    rather than in a requirements file.
    """
    result = script.pip(
        'install', '--no-index', '-f', data.find_links, "upper",
    )

    # only Upper-2.0.tar.gz should get installed.
    egg_folder = script.site_packages / 'Upper-1.0-py%s.egg-info' % pyversion
    assert egg_folder not in result.files_created, str(result)
    egg_folder = script.site_packages / 'Upper-2.0-py%s.egg-info' % pyversion
    assert egg_folder in result.files_created, str(result)


def test_url_incorrect_case_file_index(script, data):
    """
    Same as test_url_req_case_mismatch_file_index, except testing for the case
    where the incorrect case is given in the name of the package to install
    rather than in a requirements file.
    """
    result = script.pip(
        'install', '--index-url', data.find_links3, "dinner",
        expect_stderr=True,
    )

    # only Upper-2.0.tar.gz should get installed.
    egg_folder = script.site_packages / 'Dinner-1.0-py%s.egg-info' % pyversion
    assert egg_folder not in result.files_created, str(result)
    egg_folder = script.site_packages / 'Dinner-2.0-py%s.egg-info' % pyversion
    assert egg_folder in result.files_created, str(result)

    # Should show index-url location in output
    assert "Looking in indexes: " in result.stdout
    assert "Looking in links: " not in result.stdout


@pytest.mark.network
def test_compiles_pyc(script):
    """
    Test installing with --compile on
    """
    del script.environ["PYTHONDONTWRITEBYTECODE"]
    script.pip("install", "--compile", "--no-binary=:all:", "INITools==0.2")

    # There are many locations for the __init__.pyc file so attempt to find
    #   any of them
    exists = [
        os.path.exists(script.site_packages_path / "initools/__init__.pyc"),
    ]

    exists += glob.glob(
        script.site_packages_path / "initools/__pycache__/__init__*.pyc"
    )

    assert any(exists)


@pytest.mark.network
def test_no_compiles_pyc(script):
    """
    Test installing from wheel with --compile on
    """
    del script.environ["PYTHONDONTWRITEBYTECODE"]
    script.pip("install", "--no-compile", "--no-binary=:all:", "INITools==0.2")

    # There are many locations for the __init__.pyc file so attempt to find
    #   any of them
    exists = [
        os.path.exists(script.site_packages_path / "initools/__init__.pyc"),
    ]

    exists += glob.glob(
        script.site_packages_path / "initools/__pycache__/__init__*.pyc"
    )

    assert not any(exists)


def test_install_upgrade_editable_depending_on_other_editable(script):
    script.scratch_path.joinpath("pkga").mkdir()
    pkga_path = script.scratch_path / 'pkga'
    pkga_path.joinpath("setup.py").write_text(textwrap.dedent("""
        from setuptools import setup
        setup(name='pkga',
              version='0.1')
    """))
    script.pip('install', '--editable', pkga_path)
    result = script.pip('list', '--format=freeze')
    assert "pkga==0.1" in result.stdout

    script.scratch_path.joinpath("pkgb").mkdir()
    pkgb_path = script.scratch_path / 'pkgb'
    pkgb_path.joinpath("setup.py").write_text(textwrap.dedent("""
        from setuptools import setup
        setup(name='pkgb',
              version='0.1',
              install_requires=['pkga'])
    """))
    script.pip('install', '--upgrade', '--editable', pkgb_path, '--no-index')
    result = script.pip('list', '--format=freeze')
    assert "pkgb==0.1" in result.stdout


def test_install_subprocess_output_handling(script, data):
    args = ['install', data.src.joinpath('chattymodule')]

    # Regular install should not show output from the chatty setup.py
    result = script.pip(*args)
    assert 0 == result.stdout.count("HELLO FROM CHATTYMODULE")
    script.pip("uninstall", "-y", "chattymodule")

    # With --verbose we should show the output.
    # Only count examples with sys.argv[1] == egg_info, because we call
    # setup.py multiple times, which should not count as duplicate output.
    result = script.pip(*(args + ["--verbose"]), expect_stderr=True)
    assert 1 == result.stderr.count("HELLO FROM CHATTYMODULE egg_info")
    script.pip("uninstall", "-y", "chattymodule")

    # If the install fails, then we *should* show the output... but only once,
    # even if --verbose is given.
    result = script.pip(*(args + ["--global-option=--fail"]),
                        expect_error=True)
    assert 1 == result.stderr.count("I DIE, I DIE")

    result = script.pip(*(args + ["--global-option=--fail", "--verbose"]),
                        expect_error=True)
    assert 1 == result.stderr.count("I DIE, I DIE")


def test_install_log(script, data, tmpdir):
    # test that verbose logs go to "--log" file
    f = tmpdir.joinpath("log.txt")
    args = ['--log=%s' % f,
            'install', data.src.joinpath('chattymodule')]
    result = script.pip(*args)
    assert 0 == result.stdout.count("HELLO FROM CHATTYMODULE")
    with open(f, 'r') as fp:
        # one from egg_info, one from install
        assert 2 == fp.read().count("HELLO FROM CHATTYMODULE")


def test_install_topological_sort(script, data):
    args = ['install', 'TopoRequires4', '--no-index', '-f', data.packages]
    res = str(script.pip(*args, expect_error=False))
    order1 = 'TopoRequires, TopoRequires2, TopoRequires3, TopoRequires4'
    order2 = 'TopoRequires, TopoRequires3, TopoRequires2, TopoRequires4'
    assert order1 in res or order2 in res, res


def test_install_wheel_broken(script, with_wheel):
    res = script.pip_install_local('wheelbroken', expect_stderr=True)
    assert "Successfully installed wheelbroken-0.1" in str(res), str(res)


def test_cleanup_after_failed_wheel(script, with_wheel):
    res = script.pip_install_local('wheelbrokenafter', expect_stderr=True)
    # One of the effects of not cleaning up is broken scripts:
    script_py = script.bin_path / "script.py"
    assert script_py.exists(), script_py
    shebang = open(script_py, 'r').readline().strip()
    assert shebang != '#!python', shebang
    # OK, assert that we *said* we were cleaning up:
    assert "Running setup.py clean for wheelbrokenafter" in str(res), str(res)


def test_install_builds_wheels(script, data, with_wheel):
    # We need to use a subprocess to get the right value on Windows.
    res = script.run('python', '-c', (
        'from pip._internal.utils import appdirs; '
        'print(appdirs.user_cache_dir("pip"))'
    ))
    wheels_cache = os.path.join(res.stdout.rstrip('\n'), 'wheels')
    # NB This incidentally tests a local tree + tarball inputs
    # see test_install_editable_from_git_autobuild_wheel for editable
    # vcs coverage.
    to_install = data.packages.joinpath('requires_wheelbroken_upper')
    res = script.pip(
        'install', '--no-index', '-f', data.find_links,
        to_install, expect_stderr=True)
    expected = ("Successfully installed requires-wheelbroken-upper-0"
                " upper-2.0 wheelbroken-0.1")
    # Must have installed it all
    assert expected in str(res), str(res)
    wheels = []
    for top, dirs, files in os.walk(wheels_cache):
        wheels.extend(files)
    # and built wheels for upper and wheelbroken
    assert "Building wheel for upper" in str(res), str(res)
    assert "Building wheel for wheelb" in str(res), str(res)
    # Wheels are built for local directories, but not cached.
    assert "Building wheel for requir" in str(res), str(res)
    # wheelbroken has to run install
    # into the cache
    assert files_in_cache != [], str(res)
    # and installed from the wheel
    assert "Running setup.py install for upper" not in str(res), str(res)
    # Wheels are built for local directories, but not cached.
    assert "Running setup.py install for requir" not in str(res), str(res)
    # wheelbroken has to run install
    assert "Running setup.py install for wheelb" in str(res), str(res)
    # We want to make sure we used the correct implementation tag
    assert wheels == [
        "Upper-2.0-{}-none-any.whl".format(pep425tags.implementation_tag),
    ]


def test_install_no_binary_disables_building_wheels(script, data, with_wheel):
    to_install = data.packages.joinpath('requires_wheelbroken_upper')
    res = script.pip(
        'install', '--no-index', '--no-binary=upper', '-f', data.find_links,
        to_install, expect_stderr=True)
    expected = ("Successfully installed requires-wheelbroken-upper-0"
                " upper-2.0 wheelbroken-0.1")
    # Must have installed it all
    assert expected in str(res), str(res)
    # and built wheels for wheelbroken only
    assert "Building wheel for wheelb" in str(res), str(res)
    # Wheels are built for local directories, but not cached across runs
    assert "Building wheel for requir" in str(res), str(res)
    # Don't build wheel for upper which was blacklisted
    assert "Building wheel for upper" not in str(res), str(res)
    # Wheels are built for local directories, but not cached across runs
    assert "Running setup.py install for requir" not in str(res), str(res)
    # And these two fell back to sdist based installed.
    assert "Running setup.py install for wheelb" in str(res), str(res)
    assert "Running setup.py install for upper" in str(res), str(res)


@pytest.mark.network
def test_install_no_binary_builds_pep_517_wheel(script, data, with_wheel):
    to_install = data.packages.joinpath('pep517_setup_and_pyproject')
    res = script.pip(
        'install', '--no-binary=:all:', '-f', data.find_links, to_install
    )
    expected = ("Successfully installed pep517-setup-and-pyproject")
    # Must have installed the package
    assert expected in str(res), str(res)

    assert "Building wheel for pep517-setup" in str(res), str(res)
    assert "Running setup.py install for pep517-set" not in str(res), str(res)


@pytest.mark.network
def test_install_no_binary_uses_local_backend(
        script, data, with_wheel, tmpdir):
    to_install = data.packages.joinpath('pep517_wrapper_buildsys')
    script.environ['PIP_TEST_MARKER_FILE'] = marker = str(tmpdir / 'marker')
    res = script.pip(
        'install', '--no-binary=:all:', '-f', data.find_links, to_install
    )
    expected = "Successfully installed pep517-wrapper-buildsys"
    # Must have installed the package
    assert expected in str(res), str(res)

    assert os.path.isfile(marker), "Local PEP 517 backend not used"


def test_install_no_binary_disables_cached_wheels(script, data, with_wheel):
    # Seed the cache
    script.pip(
        'install', '--no-index', '-f', data.find_links,
        'upper')
    script.pip('uninstall', 'upper', '-y')
    res = script.pip(
        'install', '--no-index', '--no-binary=:all:', '-f', data.find_links,
        'upper', expect_stderr=True)
    assert "Successfully installed upper-2.0" in str(res), str(res)
    # No wheel building for upper, which was blacklisted
    assert "Building wheel for upper" not in str(res), str(res)
    # Must have used source, not a cached wheel to install upper.
    assert "Running setup.py install for upper" in str(res), str(res)


def test_install_editable_with_wrong_egg_name(script):
    script.scratch_path.joinpath("pkga").mkdir()
    pkga_path = script.scratch_path / 'pkga'
    pkga_path.joinpath("setup.py").write_text(textwrap.dedent("""
        from setuptools import setup
        setup(name='pkga',
              version='0.1')
    """))
    result = script.pip(
        'install', '--editable', 'file://%s#egg=pkgb' % pkga_path
    )
    assert ("Generating metadata for package pkgb produced metadata "
            "for project name pkga. Fix your #egg=pkgb "
            "fragments.") in result.stderr
    assert "Successfully installed pkga" in str(result), str(result)


def test_install_tar_xz(script, data):
    try:
        import lzma  # noqa
    except ImportError:
        pytest.skip("No lzma support")
    res = script.pip('install', data.packages / 'singlemodule-0.0.1.tar.xz')
    assert "Successfully installed singlemodule-0.0.1" in res.stdout, res


def test_install_tar_lzma(script, data):
    try:
        import lzma  # noqa
    except ImportError:
        pytest.skip("No lzma support")
    res = script.pip('install', data.packages / 'singlemodule-0.0.1.tar.lzma')
    assert "Successfully installed singlemodule-0.0.1" in res.stdout, res


def test_double_install(script):
    """
    Test double install passing with two same version requirements
    """
    result = script.pip('install', 'pip', 'pip',
                        expect_error=False)
    msg = "Double requirement given: pip (already in pip, name='pip')"
    assert msg not in result.stderr


def test_double_install_fail(script):
    """
    Test double install failing with two different version requirements
    """
    result = script.pip('install', 'pip==*', 'pip==7.1.2', expect_error=True)
    msg = ("Double requirement given: pip==7.1.2 (already in pip==*, "
           "name='pip')")
    assert msg in result.stderr


def _get_expected_error_text():
    return (
        "Package 'pkga' requires a different Python: {} not in '<1.0'"
    ).format('.'.join(map(str, sys.version_info[:3])))


def test_install_incompatible_python_requires(script):
    script.scratch_path.joinpath("pkga").mkdir()
    pkga_path = script.scratch_path / 'pkga'
    pkga_path.joinpath("setup.py").write_text(textwrap.dedent("""
        from setuptools import setup
        setup(name='pkga',
              python_requires='<1.0',
              version='0.1')
    """))
    result = script.pip('install', pkga_path, expect_error=True)
    assert _get_expected_error_text() in result.stderr, str(result)


def test_install_incompatible_python_requires_editable(script):
    script.scratch_path.joinpath("pkga").mkdir()
    pkga_path = script.scratch_path / 'pkga'
    pkga_path.joinpath("setup.py").write_text(textwrap.dedent("""
        from setuptools import setup
        setup(name='pkga',
              python_requires='<1.0',
              version='0.1')
    """))
    result = script.pip(
        'install', '--editable=%s' % pkga_path, expect_error=True)
    assert _get_expected_error_text() in result.stderr, str(result)


def test_install_incompatible_python_requires_wheel(script, with_wheel):
    script.scratch_path.joinpath("pkga").mkdir()
    pkga_path = script.scratch_path / 'pkga'
    pkga_path.joinpath("setup.py").write_text(textwrap.dedent("""
        from setuptools import setup
        setup(name='pkga',
              python_requires='<1.0',
              version='0.1')
    """))
    script.run(
        'python', 'setup.py', 'bdist_wheel', '--universal', cwd=pkga_path)
    result = script.pip('install', './pkga/dist/pkga-0.1-py2.py3-none-any.whl',
                        expect_error=True)
    assert _get_expected_error_text() in result.stderr, str(result)


def test_install_compatible_python_requires(script):
    script.scratch_path.joinpath("pkga").mkdir()
    pkga_path = script.scratch_path / 'pkga'
    pkga_path.joinpath("setup.py").write_text(textwrap.dedent("""
        from setuptools import setup
        setup(name='pkga',
              python_requires='>1.0',
              version='0.1')
    """))
    res = script.pip('install', pkga_path)
    assert "Successfully installed pkga-0.1" in res.stdout, res


@pytest.mark.network
def test_install_pep508_with_url(script):
    res = script.pip(
        'install', '--no-index',
        'packaging@https://files.pythonhosted.org/packages/2f/2b/'
        'c681de3e1dbcd469537aefb15186b800209aa1f299d933d23b48d85c9d56/'
        'packaging-15.3-py2.py3-none-any.whl#sha256='
        'ce1a869fe039fbf7e217df36c4653d1dbe657778b2d41709593a0003584405f4'
    )
    assert "Successfully installed packaging-15.3" in str(res), str(res)


@pytest.mark.network
def test_install_pep508_with_url_in_install_requires(script):
    pkga_path = create_test_package_with_setup(
        script, name='pkga', version='1.0',
        install_requires=[
            'packaging@https://files.pythonhosted.org/packages/2f/2b/'
            'c681de3e1dbcd469537aefb15186b800209aa1f299d933d23b48d85c9d56/'
            'packaging-15.3-py2.py3-none-any.whl#sha256='
            'ce1a869fe039fbf7e217df36c4653d1dbe657778b2d41709593a0003584405f4'
        ],
    )
    res = script.pip('install', pkga_path)
    assert "Successfully installed packaging-15.3" in str(res), str(res)


@pytest.mark.network
@pytest.mark.parametrize('index', (PyPI.simple_url, TestPyPI.simple_url))
def test_install_from_test_pypi_with_ext_url_dep_is_blocked(script, index):
    res = script.pip(
        'install',
        '--index-url',
        index,
        'pep-508-url-deps',
        expect_error=True,
    )
    error_message = (
        "Packages installed from PyPI cannot depend on packages "
        "which are not also hosted on PyPI."
    )
    error_cause = (
        "pep-508-url-deps depends on sampleproject@ "
        "https://github.com/pypa/sampleproject/archive/master.zip"
    )
    assert res.returncode == 1
    assert error_message in res.stderr, str(res)
    assert error_cause in res.stderr, str(res)


def test_installing_scripts_outside_path_prints_warning(script):
    result = script.pip_install_local(
        "--prefix", script.scratch_path, "script_wheel1"
    )
    assert "Successfully installed script-wheel1" in result.stdout, str(result)
    assert "--no-warn-script-location" in result.stderr


def test_installing_scripts_outside_path_can_suppress_warning(script):
    result = script.pip_install_local(
        "--prefix", script.scratch_path, "--no-warn-script-location",
        "script_wheel1"
    )
    assert "Successfully installed script-wheel1" in result.stdout, str(result)
    assert "--no-warn-script-location" not in result.stderr


def test_installing_scripts_on_path_does_not_print_warning(script):
    result = script.pip_install_local("script_wheel1")
    assert "Successfully installed script-wheel1" in result.stdout, str(result)
    assert "--no-warn-script-location" not in result.stderr


def test_installed_files_recorded_in_deterministic_order(script, data):
    """
    Ensure that we record the files installed by a package in a deterministic
    order, to make installs reproducible.
    """
    to_install = data.packages.joinpath("FSPkg")
    result = script.pip('install', to_install, expect_error=False)
    fspkg_folder = script.site_packages / 'fspkg'
    egg_info = 'FSPkg-0.1.dev0-py%s.egg-info' % pyversion
    installed_files_path = (
        script.site_packages / egg_info / 'installed-files.txt'
    )
    assert fspkg_folder in result.files_created, str(result.stdout)
    assert installed_files_path in result.files_created, str(result)

    installed_files_path = result.files_created[installed_files_path].full
    installed_files_lines = [
        p for p in Path(installed_files_path).read_text().split('\n') if p
    ]
    assert installed_files_lines == sorted(installed_files_lines)


def test_install_conflict_results_in_warning(script, data):
    pkgA_path = create_test_package_with_setup(
        script,
        name='pkgA', version='1.0', install_requires=['pkgb == 1.0'],
    )
    pkgB_path = create_test_package_with_setup(
        script,
        name='pkgB', version='2.0',
    )

    # Install pkgA without its dependency
    result1 = script.pip('install', '--no-index', pkgA_path, '--no-deps')
    assert "Successfully installed pkgA-1.0" in result1.stdout, str(result1)

    # Then install an incorrect version of the dependency
    result2 = script.pip(
        'install', '--no-index', pkgB_path, allow_stderr_error=True,
    )
    assert "pkga 1.0 has requirement pkgb==1.0" in result2.stderr, str(result2)
    assert "Successfully installed pkgB-2.0" in result2.stdout, str(result2)


def test_install_conflict_warning_can_be_suppressed(script, data):
    pkgA_path = create_test_package_with_setup(
        script,
        name='pkgA', version='1.0', install_requires=['pkgb == 1.0'],
    )
    pkgB_path = create_test_package_with_setup(
        script,
        name='pkgB', version='2.0',
    )

    # Install pkgA without its dependency
    result1 = script.pip('install', '--no-index', pkgA_path, '--no-deps')
    assert "Successfully installed pkgA-1.0" in result1.stdout, str(result1)

    # Then install an incorrect version of the dependency; suppressing warning
    result2 = script.pip(
        'install', '--no-index', pkgB_path, '--no-warn-conflicts'
    )
    assert "Successfully installed pkgB-2.0" in result2.stdout, str(result2)


def test_target_install_ignores_distutils_config_install_prefix(script):
    prefix = script.scratch_path / 'prefix'
    distutils_config = Path(os.path.expanduser('~'),
                            'pydistutils.cfg' if sys.platform == 'win32'
                            else '.pydistutils.cfg')
    distutils_config.write_text(textwrap.dedent(
        '''
        [install]
        prefix=%s
        ''' % str(prefix)))
    target = script.scratch_path / 'target'
    result = script.pip_install_local('simplewheel', '-t', target)

    assert "Successfully installed simplewheel" in result.stdout

    relative_target = os.path.relpath(target, script.base_path)
    relative_script_base = os.path.relpath(prefix, script.base_path)
    assert relative_target in result.files_created
    assert relative_script_base not in result.files_created


@pytest.mark.incompatible_with_test_venv
def test_user_config_accepted(script):
    # user set in the config file is parsed as 0/1 instead of True/False.
    # Check that this doesn't cause a problem.
    config_file = script.scratch_path / 'pip.conf'
    script.environ['PIP_CONFIG_FILE'] = str(config_file)
    config_file.write_text("[install]\nuser = true")
    result = script.pip_install_local('simplewheel')

    assert "Successfully installed simplewheel" in result.stdout

    relative_user = os.path.relpath(script.user_site_path, script.base_path)
    assert join(relative_user, 'simplewheel') in result.files_created


@pytest.mark.network
@pytest.mark.skipif("sys.platform != 'win32'")
@pytest.mark.parametrize('pip_name', [
    'pip',
    'pip{}'.format(sys.version_info[0]),
    'pip{}.{}'.format(*sys.version_info[:2]),
    'pip.exe',
    'pip{}.exe'.format(sys.version_info[0]),
    'pip{}.{}.exe'.format(*sys.version_info[:2])
])
def test_protect_pip_from_modification_on_windows(script, pip_name):
    """
    Test that pip modification command using ``pip install ...``
    raises an error on Windows.
    """
    command = [pip_name, 'install', 'pip != {}'.format(pip_current_version)]
    result = script.run(*command, expect_error=True)
    new_command = [sys.executable, '-m', 'pip'] + command[1:]
    expected_message = (
        'To modify pip, please run the following command:\n{}'
        .format(' '.join(new_command))
    )
    assert expected_message in result.stderr, str(result)


@pytest.mark.network
@pytest.mark.skipif("sys.platform != 'win32'")
def test_protect_pip_from_modification_via_deps_on_windows(script):
    """
    Test ``pip install pkga`` raises an error on Windows
    if `pkga` implicitly tries to upgrade pip.
    """
    pkga_wheel_path = create_basic_wheel_for_package(
        script,
        'pkga', '0.1',
        depends=['pip != {}'.format(pip_current_version)],
    )

    # Make sure pip install pkga raises an error
    args = ['install', pkga_wheel_path]
    result = script.pip(*args, expect_error=True, use_module=False)
    new_command = [sys.executable, '-m', 'pip'] + args
    expected_message = (
        'To modify pip, please run the following command:\n{}'
        .format(' '.join(new_command))
    )
    assert expected_message in result.stderr, str(result)


@pytest.mark.network
@pytest.mark.skipif("sys.platform != 'win32'")
def test_protect_pip_from_modification_via_sub_deps_on_windows(script):
    """
    Test ``pip install pkga`` raises an error on Windows
    if sub-dependencies of `pkga` implicitly tries to upgrade pip.
    """
    # Make a wheel for pkga which requires pkgb
    pkga_wheel_path = create_basic_wheel_for_package(
        script,
        'pkga', '0.1',
        depends=['pkgb'],
    )

    # Make a wheel for pkgb which requires pip
    pkgb_wheel_path = create_basic_wheel_for_package(
        script,
        'pkgb', '0.1',
        depends=['pip != {}'.format(pip_current_version)],
    )

    # Make sure pip install pkga raises an error
    args = [
        'install', pkga_wheel_path, '--find-links', pkgb_wheel_path.parent
    ]
    result = script.pip(*args, expect_error=True, use_module=False)
    new_command = [sys.executable, '-m', 'pip'] + args
    expected_message = (
        'To modify pip, please run the following command:\n{}'
        .format(' '.join(new_command))
    )
    assert expected_message in result.stderr, str(result)


@pytest.mark.parametrize(
    'install_args, expected_message', [
        ([], 'Requirement already satisfied: pip in'),
        (['--upgrade'], 'Requirement already up-to-date: pip in'),
    ]
)
@pytest.mark.parametrize("use_module", [True, False])
def test_install_pip_does_not_modify_pip_when_satisfied(
        script, install_args, expected_message, use_module):
    """
    Test it doesn't upgrade the pip if it already satisfies the requirement.
    """
    result = script.pip_install_local(
        'pip', *install_args, use_module=use_module
    )
    assert expected_message in result.stdout, str(result)


def test_ignore_yanked_file(script, data):
    """
    Test ignore a "yanked" file.
    """
    result = script.pip(
        'install', 'simple',
        '--index-url', data.index_url('yanked'),
    )
    # Make sure a "yanked" release is ignored
    assert 'Successfully installed simple-2.0\n' in result.stdout, str(result)


def test_install_yanked_file_and_print_warning(script, data):
    """
    Test install a "yanked" file and print a warning.

    Yanked files are always ignored, unless they are the only file that
    matches a version specifier that "pins" to an exact version (PEP 592).
    """
    result = script.pip(
        'install', 'simple==3.0',
        '--index-url', data.index_url('yanked'),
        expect_stderr=True,
    )
    expected_warning = 'Reason for being yanked: test reason message'
    assert expected_warning in result.stderr, str(result)
    # Make sure a "yanked" release is installed
    assert 'Successfully installed simple-3.0\n' in result.stdout, str(result)


@pytest.mark.parametrize("install_args", [
    (),
    ("--trusted-host", "localhost"),
])
def test_install_sends_client_cert(install_args, script, cert_factory, data):
    cert_path = cert_factory()
    ctx = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
    ctx.load_cert_chain(cert_path, cert_path)
    ctx.load_verify_locations(cafile=cert_path)
    ctx.verify_mode = ssl.CERT_REQUIRED

    server = make_mock_server(ssl_context=ctx)
    server.mock.side_effect = [
        package_page({
            "simple-3.0.tar.gz": "/files/simple-3.0.tar.gz",
        }),
        file_response(str(data.packages / "simple-3.0.tar.gz")),
    ]

    url = "https://{}:{}/simple".format(server.host, server.port)

    args = ["install", "-vvv", "--cert", cert_path, "--client-cert", cert_path]
    args.extend(["--index-url", url])
    args.extend(install_args)
    args.append("simple")

    with server_running(server):
        script.pip(*args)

    assert server.mock.call_count == 2
    for call_args in server.mock.call_args_list:
        environ, _ = call_args.args
        assert "SSL_CLIENT_CERT" in environ
        assert environ["SSL_CLIENT_CERT"]
