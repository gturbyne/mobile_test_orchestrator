"""
This package contains classes associated with an application installed on the device.
An application is distinguished from a bundle (an apk).  A bundle is a package that only after installed
creates an application on the target device.  Thus, an application only makes sense in the context of the
device on which it can be launched.

The base class for applications is *Application* with *ServiceApplication* and *TestApplication* inheriting
from that class to provide additional APIs specific to service application and (Espresso) test applications.
"""

import asyncio
import logging
import subprocess
import time
from contextlib import suppress

from apk_bitminer.parsing import AXMLParser  # type: ignore
from typing import List, TypeVar, Type, Optional, AsyncContextManager, Dict, Union, Set, Tuple, Callable, Iterable

from .device import Device, DeviceBased, _device_lock, DeviceInteraction

__all__ = ["Application", "ServiceApplication", "TestApplication"]


log = logging.getLogger(__name__)

# for returning instance of "cls" in install class-method.  Recommended way of doing this, but there
# may be better way in later Python 3 interpreters?
_TApp = TypeVar('_TApp', bound='Application')
_TTestApp = TypeVar('_TTestApp', bound='TestApplication')


class Application(DeviceBased):
    """
    Defines an application installed on a remotely USB-connected device. Provides an interface for stopping,
    starting an application, and such. It is recommended to create instances via the class-method `from_apk` or
    `from_apk_async`

    :param manifest: AXMLParser instance representing manifest from an apk, or Dict of key/value string pairs of
       package name and permissions for app;  if Dict, the dictionary MUST contain "package_name" as a key, and
       optionally contain "permissions" as a key (otherwise assumed to be empty)
    :param device: which device app resides on

    >>> async def install(device:Device):
    ...     app = await Application.from_apk_async("some.apk", device)
    ...     app.grant_permissions(["android.permission.WRITE_EXTERNAL_STORAGE"])

    or the non-async version

    >>> def install(device: Device):
    ...    app = Application.from_apk("some.apk", device)
    ...    app.grant_permissions(["android.permission.WRITE_EXTERNAL_STORAGE"])
    """

    SILENT_RUNNING_PACKAGES = ["com.samsung.android.mtpapplication", "com.wssyncmldm", "com.bitbar.testdroid.monitor"]
    SLEEP_GRANT_PERMISSION = 4

    def __init__(self, device: Device, manifest: Union[AXMLParser, Dict[str, str]]):
        super().__init__(device)
        self._device_navigation = DeviceInteraction(device)
        self._version: Optional[str] = None  # loaded on-demand first time self.version called
        self._package_name: str = manifest.package_name if isinstance(manifest, AXMLParser) \
            else manifest.get("package_name", "")
        if not self._package_name:
            raise ValueError("manifest argument as dictionary must contain \"package_name\" as key")
        self._permissions: Set[str] = set(manifest.permissions if isinstance(manifest, AXMLParser) else
                                          manifest.get("permissions", []))
        self._granted_permissions: Set[str] = set()

    @classmethod
    def verify_install(cls, device: Device, package: str) -> None:
        """
        Verify installation of an app, taking a screenshot on failure

        :param device: device to install on
        :param package: package name of app
        :raises Exception: if failure to verify
        """
        packages = device.list_installed_packages()
        if package not in packages:
            # some devices (may??) need time for package install to be detected by system
            time.sleep(Device.SLEEP_PKG_INSTALL)
            packages = device.list_installed_packages()
        if package not in packages:
            if package is not None:
                log.error("Did not find installed package %s;  found: %s", package, packages)
                log.error("Device failure to install %s on model %s;  install status succeeds,"
                          "but package not found on device", package, device.model)
            raise Exception("Failed to verify installation of app '%s', event though output indicated otherwise" %
                            package)
        else:
            log.info("Package %s installed" % str(package))

    @classmethod
    def _install(cls, device: Device, apk_path: str, *args: str) -> None:
        """
        Install the given bundle, blocking until complete
        Preference should be to use `Application.from_apk_async` method.

        :param device: device to install on
        :param apk_path: local path to the apk to be installed
        :param as_upgrade: install as upgrade or not
        """
        cmd: Tuple[str, ...] = ("install", *args, apk_path)
        device.execute_remote_cmd(*cmd, timeout=Device.TIMEOUT_LONG_ADB_CMD)

    @classmethod
    async def _monitor_install(cls, device: Device, apk_path: str, *args: str,
                               callback: Optional[Callable[[Device.AsyncProcessContext], None]] = None) -> None:
        """
        Install given apk asynchronously, monitoring output for messages containing any of the given conditions,
        executing a callback if given when any such condition is met.

        :param device: Device to install on
        :param apk_path: bundle to install
        :raises Device.InsufficientStorageError: if there is not enough space on device
        :raises IOError if push of apk to device was unsuccessful
        """
        parser = AXMLParser.parse(apk_path)
        package = parser.package_name
        remote_data_path = f"/data/local/tmp/{package}"
        try:
            try:
                # We try Android Studio's method of pushing to device and installing from there, but if push is
                # unsuccessful, we fallback to plain adb install
                push_cmd = ("push", apk_path, remote_data_path)
                device.execute_remote_cmd(*push_cmd, timeout=Device.TIMEOUT_LONG_ADB_CMD)
            except Device.CommandExecutionFailure as e:
                raise IOError(f"Unable to push apk to device {e}") from e

            # Execute the installation of the app, monitoring output for completion in order to invoke any extra
            # commands or detect insufficient storage issues
            cmd: List[str] = ["shell", "pm", "install"] + list(args) + [remote_data_path]
            # Do not allow more than one install at a time on a specific device, as this can be problematic
            async with _device_lock(device), device.monitor_remote_cmd(*cmd) as proc:
                if callback:
                    callback(proc)
                await proc.wait(timeout=Device.TIMEOUT_LONG_ADB_CMD)
                if proc.returncode != 0:
                    raise Device.CommandExecutionFailure(proc.returncode,
                                                         f"Install of {apk_path} failed: {await proc.communicate()}")
        finally:
            with suppress(Exception):
                rm_cmd = ("shell", "rm", remote_data_path)
                device.execute_remote_cmd(*rm_cmd, timeout=Device.TIMEOUT_ADB_CMD)

    @property
    def package_name(self) -> str:
        """
        :return: Android package name associated with this app
        """
        return self._package_name

    @property
    def permissions(self) -> Set[str]:
        """
        :return: set of permissions required by this app
        """
        return self._permissions

    @property
    def version(self) -> Optional[str]:
        """
        :return: version of this app
        """
        if self._version is None:
            self._version = self.device.get_version(self.package_name)
        return self._version

    @property
    def pid(self) -> Optional[str]:
        """
        :return: pid of app if running (either in foreground or background) or None if not running
        """
        completed = self.device.execute_remote_cmd("shell", "pidof", "-s", self.package_name, stdout=subprocess.PIPE,
                                                   fail_on_error_code=lambda x: False)
        stdout: str = completed.stdout
        split_output = stdout.splitlines()
        if split_output:
            return split_output[0].strip()
        return None

    @property
    def granted_permissions(self) -> Set[str]:
        """
        :return: set of all permissions granted to the app
        """
        return set(self._granted_permissions)

    @classmethod
    async def from_apk_async(cls: Type[_TApp], apk_path: str, device: Device, as_upgrade: bool = False,
                             callback: Optional[Callable[[Device.AsyncProcessContext], None]] = None) -> _TApp:
        """
        Install application asynchronously.  This allows the output of the install to be processed
        in a streamed fashion.  This can be useful on some devices that are non-standard android where installs
        cause (for example) a pop-up requesting direct user permission for the install -- monitoring for a specific
        message to simulate the tap to confirm the install.

        :param apk_path: path to apk
        :param device: device to install on
        :param as_upgrade: whether to install as upgrade or not
        :param callback: callback invoked after push of apk to device and kick-off of pm-install through adb;  callback
            should return None and take one parameter -- a `Device.Process` instance that can be used to wait on the
            process, parse streamed stdout, etc.
        :return: remote installed application
        :raises: Exception if failure of install or verify installation

        >>> async def install():
        ...     async with Application.from_apk_async("/some/local/path/to/apk", device) as stdout:
        ...         async for line in stdout:
        ...            if "some trigger message" in line:
        ...               perform_tap_to_accept_install()

        """
        parser = AXMLParser.parse(apk_path)
        args = ["-r"] if as_upgrade else []
        await cls._monitor_install(device, apk_path, *args, callback=callback)
        return cls(device, parser)

    @classmethod
    def from_apk(cls: Type[_TApp], apk_path: str, device: Device, as_upgrade: bool = False) -> _TApp:
        """
        Install provided application, blocking until install is complete

        :param apk_path: path to apk
        :param device: device to install on
        :param as_upgrade: whether to install as upgrade or not

        :return: remote installed application

        :raises: Exception if failure ot install or verify installation

        >>> app = Application.from_apk("/local/path/to/apk", device, as_upgrade=True)
        """
        parser = AXMLParser.parse(apk_path)
        args = ["-r"] if as_upgrade else []
        cls._install(device, apk_path, *args)
        return cls(device, parser)

    def uninstall(self) -> None:
        """
        Uninstall this app from remote device.
        """
        self.stop()
        try:
            self.device.execute_remote_cmd("uninstall", self.package_name)
        except subprocess.TimeoutExpired:
            log.warning("adb command froze on uninstall.  Ignoring issue as device specific")
        except Exception as e:
            if self.package_name in self.device.list_installed_packages():
                log.error(f"Failed to uninstall app {self.package_name} [{str(e)}]")

    def grant_permissions(self, permissions: Optional[Iterable[str]] = None) -> Set[str]:
        """
        Grant permissions for a package on a device

        :param permissions: string list of Android permissions to be granted, or None to grant app's defined
           user permissions

        :return: the list of all permissions granted to the app
        """
        permissions = permissions or self.permissions
        # workaround for xiaomi:
        permissions_filtered = set(p.strip() for p in permissions if p in Device.DANGEROUS_PERMISSIONS)
        if not permissions_filtered:
            log.info("Permissions %s already requested or no 'dangerous' permissions requested, so nothing to do" %
                     permissions)
            return set()
        # note "block grants" do not work on all Android devices, so grant 'em one-by-one
        for p in permissions_filtered:
            try:
                self.device.execute_remote_cmd("shell", "pm", "grant", self.package_name, p)
            except Exception as e:
                log.error(f"Failed to grant permission {p} for package {self.package_name} [{str(e)}]")
            self._granted_permissions.add(p)
        return self._granted_permissions

    def regrant_permissions(self) -> Set[str]:
        """
        Regrant permissions (e.g. if an app's data was cleared) that were previously granted

        :return: list of permissions that are currently granted to the app
        """
        return self.grant_permissions(self._granted_permissions)

    def start(self, activity: str, *options: str, intent: Optional[str] = None) -> None:
        """
        start an app on the device

        :param activity: which Android Activity to invoke on start of app
        :param intent: which Intent to invoke, or None for default intent
        :param options: string list of options to pass on to the "am start" command on the remote device, or None
        """
        # embellish to fully qualified name as Android expects
        if '.' not in activity:
            activity = f"{self.package_name}.{activity}"
        activity = f"{self.package_name}/{activity}"
        if intent:
            options = ("-a", intent, *options)
        self.device.execute_remote_cmd("shell", "am", "start", "-n", activity, *options)

    async def launch(self, activity: str, *options: str, intent: Optional[str] = None,
                     timeout: Optional[int] = None) -> None:
        """
        start the app, monitoring logcat output until it indicates app has either Displayed or crashed

        :param activity: which Android Activity to invoke on start of app
        :param intent: which Intent to invoke, or None for default intent
        :param options: string list of options to pass on to the "am start" command on the remote device, or None
        :param timeout: if not none, timeout if no detection of startup after at least this many seconds

        :raises: Exception if app crashes during start and fails to display
        """
        async def launch() -> None:
            async with self.device.monitor_remote_cmd(
                    "logcat", "-s", "ActivityManager:I", "ActivityTaskManager:I", "AndroidRuntime:E",
                    "-T", "1") as logcat_proc:
                # catch the logcat stream first befor launching, just to be sure
                count = 0
                async for _ in logcat_proc.output(unresponsive_timeout=timeout):
                    count += 1
                    if count == 2:
                        break
                detected_fatal_exception = False
                self.start(activity, *options, intent=intent)
                async for line in logcat_proc.output(unresponsive_timeout=timeout):
                    if "FATAL EXCEPTION" in line:
                        detected_fatal_exception = True
                    if detected_fatal_exception and "Process" in line and self.package_name in line:
                        raise Exception("App crashed on startup")
                    if "Displayed" in line and self.package_name in line:
                        break
        if timeout is not None:
            await asyncio.wait_for(launch(), timeout=timeout)
        else:
            await launch()

    def monkey(self, count: int = 1) -> None:
        """
        Run monkey against application
        More to read about adb monkey at https://developer.android.com/studio/test/monkey#command-options-reference
        """
        cmd = ["shell", "monkey", "-p", self._package_name, "-c", "android.intent.category.LAUNCHER", str(count)]
        self.device.execute_remote_cmd(*cmd, timeout=Device.TIMEOUT_LONG_ADB_CMD)

    def stop(self, force: bool = True) -> None:
        """
        stop this app on the device

        :param force: perform a force-stop if true (kill of app) rather than normal stop
        """
        try:
            if force:
                self._device_navigation.go_home()
                self.device.execute_remote_cmd("shell", "am", "force-stop", self.package_name)
            else:
                self.device.execute_remote_cmd("shell", "am", "stop", self.package_name)
        except Exception as e:
            log.error(f"Failed to force-stop app {self.package_name} with error: {str(e)}")

    def clean_kill(self) -> None:
        """
        Running this command to close the app is equivalent to the app being backgrounded, and then having the system
        kill the app to clear up resources for other apps. It is also equivalent to closing the app from the "Recent
        Apps" menu.

        Subsequent launches of the app will need to recreate the app from scratch, and is considered a
        cold app launch.
        NOTE: Currently appears to only work with Android 9.0 devices
        """
        self._device_navigation.input("KEYCODE_HOME")
        tries = 3
        # poll a few times to ensure input is enacted
        while tries and not self._device_navigation.home_screen_active():
            tries -= 1
            time.sleep(0.5)
        self.device.execute_remote_cmd("shell", "am", "kill", self.package_name)
        if self.pid is not None:
            if not self._device_navigation.home_screen_active():
                raise Exception("Failed to background current foreground app. Cannot complete app closure.")
            else:
                raise Exception("Detected app process is still running. App closure failed.")

    def clear_data(self, regrant_permissions: bool = True) -> None:
        """
        clears app data for given package
        """
        self.device.execute_remote_cmd("shell", "pm", "clear", self.package_name)
        if regrant_permissions:
            self.regrant_permissions()
        else:
            self._granted_permissions = set()

    def in_foreground(self, ignore_silent_apps: bool = True) -> bool:
        """
        :return: whether app is currently in foreground
        """
        activity_stack = self.device.get_activity_stack() or []
        index = 0
        if ignore_silent_apps:
            while activity_stack and (activity_stack[index].lower() in self.SILENT_RUNNING_PACKAGES):
                index += 1
        if index > len(activity_stack):
            return False
        foreground_activity = activity_stack[index]
        return foreground_activity.lower() == self.package_name.lower()


class ServiceApplication(Application):
    """
    Class representing an Android application that is specifically a service
    """

    def start(self, activity: str, *options: str, intent: Optional[str] = None, foreground: bool = True) -> None:

        """
        invoke an intent associated with this service by calling start the service

        :param options: string list of options to supply to "am startservice" command
        :param activity: activity handles the intent
        :param intent: if not None, invoke specific intent otherwise invoke default intent
        :param foreground: whether to start in foreground or not (Android O+
            does not allow background starts any longer)
        """
        if not activity:
            raise Exception("Must provide an activity for ServiceApplication")

        activity = f"{self.package_name}/{activity}"
        options = tuple(f'"{item}"' for item in options)
        if intent:
            options = ("-a", intent) + options
        if foreground and self.device.api_level >= 26:
            self.device.execute_remote_cmd("shell", "am", "start-foreground-service", "-n", activity, *options)
        else:
            self.device.execute_remote_cmd("shell", "am", "startservice", "-n", activity, *options)

    def broadcast(self, activity: str,  # TODO: activity should be Optional, as it is in Application.
                  *options: str, action: Optional[str]) -> None:
        """
        Invoke an intent associated with this service by broadcasting an event
        :param activity: activity that handles the intent
        :param options: string list of options to supply to "am broadcast" command
        :param action: if not None, invoke specific action
        :return:
        """
        if not activity:
            raise Exception("Must provide an activity for ServiceApplication")

        activity = f"{self.package_name}/{activity}"
        options = tuple(f'"{item}"' for item in options)
        if action:
            options = ("-a", action) + options
        self.device.execute_remote_cmd("shell", "am", "broadcast", "-n", activity, *options)


class TestApplication(Application):
    """
    Class representing an Android test application installed on a remote device
    It is recommended to  create instances via the class-method `from_apk` or `from_apk_async`:

    >>> async def install_test_app(device: Device):
    ...    await test_app = TestApplication.from_apk("some.apk", device)
    ...    test_app.run()

    or the non-async version:

    >>> def install_test_app(device: Device):
    ...    test_app = TestApplication.from_apk("some.apk", device)
    ...    test_app.run()

    :param device: which device app resides on
    :param mainfest: contains info about app
    """

    def __init__(self, device: Device, manifest: AXMLParser):
        super(TestApplication, self).__init__(device=device, manifest=manifest)
        valid = (hasattr(manifest, "instrumentation") and (manifest.instrumentation is not None) and
                 bool(manifest.instrumentation.target_package) and bool(manifest.instrumentation.runner))
        if not valid:
            raise Exception("Test application's manifest does not specify proper instrumentation element."
                            "Are you sure this is a test app")
        self._runner: str = manifest.instrumentation.runner
        self._target_application = Application(device, manifest={'package_name': manifest.instrumentation.target_package})
        self._permissions = set(manifest.permissions)

    @property
    def target_application(self) -> Application:
        """
        :return: target application under test for this test app
        """
        return self._target_application

    @property
    def runner(self) -> str:
        """
        :return: runner associated with this test app
        """
        return self._runner

    def list_runners(self) -> List[str]:
        """
        :return: all test runners available for that package
        """
        items = []
        for line in self.device.list_instrumentation():
            if line and self.package_name in line:
                log.info("ADB: matched on line : %s" % line)
                runner = line.replace('instrumentation:', '').split(' ')[0].strip()
                items.append(runner)
        return items

    def run(self, *options: str) -> Device.AsyncProcessContext:
        """
        Run an instrumentation test package, yielding lines from std output
        NOTE: this returns an object implementing the AsyncContextManager interface, intended for use in
           an "async with" call, even though it is itself not a Coroutine

        :param options: arguments to pass to instrument command
        :param loop: event loop to execute under, or None for default event loop

        :returns: return coroutine wrapping an asyncio context manager for iterating over lines

        :raises Device.CommandExecutionFailureException with non-zero return code information on non-zero exit status

        >>> app = TestApplication.fromApk("some.apk", device)
        ...
        ... async def run():
        ...     async with app.run() as stdout:
        ...         async  for line in stdout:
        ...             print(line)
        """
        if self._target_application.package_name not in self.device.list_installed_packages():
            raise Exception("App under test, as designatee by this test app's manifest, is not installed!")
        # surround each arg with quotes to preserve spaces in any arguments when sent to remote device:
        options = tuple('"%s"' % arg if not arg.startswith('"') and not arg.startswith("-") else arg for arg in options)
        return self.device.monitor_remote_cmd("shell", "am", "instrument", "-w", *options, "-r",
                                              "/".join([self._package_name, self._runner]))

    def run_orchestrated(self, *options: str) -> Device.AsyncProcessContext:
        """
        Run an instrumentation test package via Google's test orchestrator that
        NOTE: this returns an object implementing the AsyncContextManager interface, intended for use in
           an "async with" call, even though it is itself not a Coroutine

        :param options: arguments to pass to instrument command
        :returns: return coroutine wrapping an asyncio context manager for iterating over lines
        :raises Device.CommandExecutionFailureException with non-zero return code information on non-zero exit status
        """
        packages = self.device.list_installed_packages()
        if not {'android.support.test.services', 'android.support.test.orchestrator'} < set(packages):
            raise Exception("Must install both test-services-<version>.apk and orchestrator-<version>.apk to run "
                            + "under Google's Android Test Orchestrator")
        if self._target_application.package_name not in self.device.list_installed_packages():
            raise Exception("App under test, as designatee by this test app's manifest, is not installed!")
        # surround each arg with quotes to preserve spaces in any arguments when sent to remote device:
        options_text = " ".join(['"%s"' % arg if not arg.startswith('"') and not arg.startswith("-") else arg
                                 for arg in options])
        return self.device.monitor_remote_cmd(
            "shell",
            "CLASSPATH=$(pm path android.support.test.services) "
            + "app_process / android.support.test.services.shellexecutor.ShellMain am instrument "
            + f"-r -w -e -v -targetInstrumentation {'/'.join([self._package_name, self._runner])} {options_text} "
            + "android.support.test.orchestrator/android.support.test.orchestrator.AndroidTestOrchestrator")

    @classmethod
    async def from_apk_async(cls: Type[_TTestApp], apk_path: str, device: Device, as_upgrade: bool = False,
                             callback: Optional[Callable[[Device.AsyncProcessContext], None]] = None) -> _TTestApp:
        """
        Install apk as a test application on the given device

        :param apk_path: path to test apk (containing a runner)
        :param device: device to install on
        :param as_upgrade: whether to install as upgrade or not
        :param callback: if not None, callback to be made on successful push and at start of install; the
           `Device.Process` that is installing from device storage is passed as the only parameter to the callback
        :return: `TestApplication` of installed apk

        >>> application = await Application.from_apk_async("local/path/to/apk", device)
        """
        parser = AXMLParser.parse(apk_path)
        args = ["-t"] if not as_upgrade else ["-r", "-t"]
        await cls._monitor_install(device, apk_path, *args, callback=callback)
        return cls(device, parser)

    @classmethod
    def from_apk(cls: Type[_TTestApp], apk_path: str, device: Device, as_upgrade: bool = False) -> _TTestApp:
        """
        Install apk as a test application on the given device

        :param apk_path: path to test apk (containing a runner)
        :param device: device to install on
        :param as_upgrade: whether to install as upgrade or not
        :return: `TestApplication` of installed apk

        >>> test_application = Application.from_apk("/local/path/to/apk", device, as_upgrade=True)
        """
        parser = AXMLParser.parse(apk_path)
        args = ["-t"] if not as_upgrade else ["-r", "-t"]
        cls._install(device, apk_path, *args)
        return cls(device, parser)
