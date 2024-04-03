#!/YOUR/MANAGED/PYTHON/INSTANCE/HASHBANG

import argparse
import errno
import json
import logging
import os
import pathlib
import platform
import signal
import subprocess
import sys
from enum import Enum
from functools import wraps

import requests
import xattr
import yaml
from CoreFoundation import (
    CFPreferencesAppSynchronize,
    CFPreferencesCopyAppValue,
    CFPreferencesCopyKeyList,
    CFPreferencesCopyValue,
    CFPreferencesSetValue,
    CFPreferencesSynchronize,
    kCFPreferencesAnyUser,
    kCFPreferencesCurrentHost,
)
from dateutil.parser import parse as datetime_parse
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version
from packaging.version import parse as version_parse
from PyObjCTools import Conversion
from SystemConfiguration import SCDynamicStoreCopyConsoleUser

__version__ = "2.0.4"
__appID__ = "com.amazon.clienteng.upgradebuddy"

# Setup ALL THE LOGS!
logger = logging.getLogger(__appID__)
file_formatter = logging.Formatter(
    "%(asctime)s%(msecs)3d - %(levelname)s: %(message)s", datefmt="%Y-%m-%dT%H:%M"
)
stdo_formatter = logging.Formatter("%(levelname)s: %(message)s")
file_handler = logging.FileHandler(pathlib.Path("/var/log/ce_upgradebuddy.log"))
stdo_handler = logging.StreamHandler()
file_handler.setFormatter(file_formatter)
stdo_handler.setFormatter(stdo_formatter)
logger.addHandler(file_handler)
logger.addHandler(stdo_handler)
logger.setLevel(logging.INFO)

try:
    import pyoslog

    pyos_handler = pyoslog.Handler()
    pyos_handler.setSubsystem(__appID__, "Execution")
    logger.addHandler(pyos_handler)
except ImportError:
    logger.warning("No pyoslog install found, skipping handler")


def timeout(seconds=10, error_message=os.strerror(errno.ETIME)):
    def decorator(func):
        def _handle_timeout(signum, frame):
            logger.error(f"Timeout Exceeded! {error_message}")
            raise TimeoutError(error_message)

        @wraps(func)
        def wrapper(*args, **kwargs):
            signal.signal(signal.SIGALRM, _handle_timeout)
            signal.alarm(seconds)
            try:
                result = func(*args, **kwargs)
            finally:
                signal.alarm(0)
            return result

        return wrapper

    return decorator


class DialogExits(Enum):
    SUCCESS = 0
    ALWAYSREQ = 0
    FAILED = 1
    BUTTON2 = 2
    INFO = 3
    TIMER = 4
    QUIT = 10
    CRITFAIL = 111


class UpgradeBuddy:
    """
    UpgradeBuddy class
    """

    def __init__(
        self,
        testosversion=None,
        testfile=None,
        ignoreupdated=False,
        cachedir=None,
        user=None,
    ):
        self.current_os_version = (
            version_parse(testosversion)
            if testosversion is not None
            else version_parse(platform.mac_ver()[0])
        )
        self.last_os_version = self.get_last_logged_verson()
        self.os_updated = (self.current_os_version > self.last_os_version) or ignoreupdated
        self.dialog_app = "/usr/local/bin/dialog"
        self.cache_dir = (
            pathlib.Path(cachedir)
            if cachedir is not None
            else pathlib.Path(self.get_preference("CacheDir", managed=True))
        )
        self.cache_dir.mkdir(exist_ok=True)
        self.asset_dir = self.cache_dir / "assets"
        self.asset_dir.mkdir(exist_ok=True)
        self.distro_url = self.get_preference("DistroURL", managed=True)
        self.local_config = pathlib.Path(self.cache_dir / "messages.yaml")
        self.remote_config = f"{self.distro_url}/messages.yaml"
        self.test_file = testfile
        self.test_os_version = testosversion
        self.ignore_os_updated = ignoreupdated
        self.current_user = user
        self.valid_user = True if user not in self.get_ignored_users() else False

    @timeout(seconds=30, error_message="Network unavailable")
    def waitfornetwork(self):
        """
        Waits for network to be available
        """
        logger.debug("Waiting for network")
        online = False
        while not online:
            try:
                requests.head(self.remote_config, timeout=5).raise_for_status()
                logger.debug("Network is available")
                online = True
            except (requests.Timeout, requests.HTTPError) as error:
                logger.error(f"Network not available: {error}")
                pass

    def get_ignored_users(self) -> list:
        """
        Gets the list of users to ignore
        """
        users = self.get_preference("IgnoredUsers", managed=True)
        return [] if users is None else users

    def get_preference(self, key, managed=False):
        """
        sets the UpgradeBuddy preference

        Args:
            key (any): key to get value from CFPreference cache
            managed (bool): use managed preferences to get key instead

        Returns:
            any: the value of the key checked
        """
        try:
            if managed:
                CFPreferencesAppSynchronize(__appID__)
                preference = CFPreferencesCopyAppValue(
                    key,
                    __appID__,
                )
                logger.debug(f"Managed Preference Got: {key},{preference}")
            else:
                CFPreferencesSynchronize(
                    __appID__, kCFPreferencesAnyUser, kCFPreferencesCurrentHost
                )
                preference = CFPreferencesCopyValue(
                    key, __appID__, kCFPreferencesAnyUser, kCFPreferencesCurrentHost
                )
                logger.debug(f"Preference Got: {key},{preference}")
            return preference
        except Exception as error:
            logger.error(f"Preference Error: {error}")
            sys.exit(1)

    def set_preference(self, key, value) -> None:
        """
        takes key and value, sets preference for upgradeBuddy

        Args:
            key (_any_): key to set
            value (_any_): value of key
        """
        CFPreferencesSetValue(
            key,
            value,
            __appID__,
            kCFPreferencesAnyUser,
            kCFPreferencesCurrentHost,
        )
        CFPreferencesSynchronize(__appID__, kCFPreferencesAnyUser, kCFPreferencesCurrentHost)

    def get_last_logged_verson(self) -> Version:
        """
        Gets the last logged OS Version that ran
        """
        last_logged = self.get_preference("lastLoggedOS")
        if last_logged is None:
            last_version = version_parse("12")
        else:
            last_version = version_parse(str(last_logged))
        return last_version

    def set_last_logged_version(self) -> None:
        """
        Sets the last logged version
        """
        logger.debug(f"Setting lastLoggedOS: {self.current_os_version}")
        self.set_preference("lastLoggedOS", str(self.current_os_version))

    def get_xattr(self, path, attr) -> None:
        """Get a named xattr from a file. Return None if not present."""
        item = None
        if attr in xattr.listxattr(path):
            item = xattr.getxattr(path, attr).decode()
            logger.debug(f"Xattr got {attr} at {path}, {item}")
        else:
            logger.debug(f"Xattr ({attr}) not found at {path}!")
        return item

    def get_latest_etag(self, url: str) -> str:
        """
        Gets latest etag from url's headers

        Args:
            url (str): URL to check etag header

        Returns:
            str: etag attribute of resource
        """
        logger.debug(f"Getting etag header for {url}")
        with requests.Session() as session:
            try:
                ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
                headers = session.head(url, timeout=20, headers={"User-Agent": ua})
                headers.raise_for_status()
                headers = headers.headers
                etag = headers.get("ETag")
                logger.debug(f"Etag Got: {etag}")
            except requests.HTTPError as httperror:
                logger.error(f"Unable to get etag! {httperror}")
                sys.exit(1)
        return etag

    def item_needs_downloaded(self, filepath: pathlib.Path, url: str) -> bool:
        """
        returns true or false if item needs to be downloaded based on if local etag is out of date

        Args:
            filepath (pathlib.Path): pathlib path to file on disk
            url (str): url resource to check against local path

        Returns:
            bool: T/F of item needing to be downloaded or if local cached item is valid
        """
        logger.info(f"Checking if {filepath} needs downloading...")
        if filepath.exists():
            logger.debug(f"{filepath} exists, checking ETag")
            latest_etag = self.get_latest_etag(url)
            file_etag = self.get_xattr(filepath, "ETag")
            if file_etag == latest_etag:
                logger.info("ETag is same, skipping download")
                return False
            logger.info("Etag is the not same")
        return True

    def download_item(self, filepath: pathlib.Path, url: str) -> None:
        """
        Downloads item from url, caches the file at filepath. Writes it's etag
        to an xattr on the file at filepath

        Returns: None
        """
        logger.info(f"Downloading file: {filepath}")
        try:
            download_item = requests.get(url, timeout=20)
            download_item.raise_for_status()
        except (TimeoutError, requests.ConnectionError, requests.HTTPError) as error:
            logger.error(f"Download failed! {error}")
            sys.exit(1)
        etag = download_item.headers.get("ETag")
        logger.debug(f"Got etag date {etag}")
        logger.info(f"Writing file to disk: {filepath}")
        filepath.write_bytes(download_item.content)
        try:
            xattr.setxattr(filepath, "ETag", bytes(etag, encoding="UTF-8"))
            logger.info(f"Set ETag date on {filepath}")
        except IOError as ioerror:
            logger.error(f"Unable to set extended attribute! {ioerror}")
            sys.exit(1)
        return filepath

    def write_markdown_file(self, id: str, version: int, message: str) -> str:
        """
        Writes a markdown file to disk, with the id and version as the filename
        """
        logger.debug(f"Writing markdown file: {id} (Version: {version})")
        markdownfile = pathlib.Path(f"{self.cache_dir}/{id}.md")
        if markdownfile.exists():
            try:
                file_vers = int(self.get_xattr(markdownfile, "version"))
                if file_vers == version:
                    logger.debug("Version is the same, skipping write")
                    return str(markdownfile.absolute())
            except TypeError as xattrerr:
                logger.error(xattrerr)
                pass
        with open(markdownfile, "w") as md:
            markdowncontent = message
            md.write(markdowncontent)
        md.close()
        xattr.setxattr(markdownfile, "version", bytes(str(version), encoding="UTF-8"))
        return str(markdownfile.absolute())

    def process_all_messages(self) -> list[dict]:
        """
        Get all messages from config and determine which are appropriate to show

        Returns:
            list[dict]: list of dicts containing the message objects to display
        """
        if self.test_file:
            logger.info("Running in test mode, not downloading config messages")
            message_config = pathlib.Path(self.test_file)
        else:
            if self.item_needs_downloaded(self.local_config, self.remote_config):
                message_config = self.download_item(self.local_config, self.remote_config)
            else:
                message_config = self.local_config
        try:
            with open(message_config, "rb") as config:
                messages = next(yaml.load_all(config, Loader=yaml.FullLoader))
        except yaml.parser.ParserError as configerr:
            logger.error(f"Failed to load configuration file! {configerr}")
            sys.exit(1)
        applicable_messages = []
        user_messages = Conversion.pythonCollectionFromPropertyList(
            self.get_preference("seenMessages")
        )
        if not user_messages:
            user_messages = {}
            user_messages[self.current_user] = {}
        logger.debug(f"User seen messages: {user_messages}")
        for message in messages:
            msg_id = message.get("messageID")
            version = message.get("messageVersion")
            for prop in message["dialogProperties"]:
                if isinstance(message["dialogProperties"][prop], str):
                    message["dialogProperties"][prop] = message["dialogProperties"][prop].replace(
                        "<<MSG>>", message["messageID"]
                    )
                    message["dialogProperties"][prop] = message["dialogProperties"][prop].replace(
                        "assets://",
                        f"{self.distro_url}/assets/",  # One Day, add Cache support to this
                    )
            logger.info(f"Checking if {msg_id}, ({version}) has been seen by {self.current_user}")
            if msg_id in user_messages[self.current_user]:
                if int(user_messages[self.current_user][msg_id]) >= version:
                    logger.info(
                        f"Message {msg_id} ({version}) has been seen by {self.current_user}"
                    )
                    continue
            mdfile = self.write_markdown_file(
                msg_id, version, message["dialogProperties"]["message"]
            )
            message["dialogProperties"]["message"] = mdfile
            if self.current_os_version in SpecifierSet(message.get("osRequirements")):
                logger.info(f"Message {msg_id} ({version}) meets the requirements to display")
                applicable_messages.append(message)

        return applicable_messages

    def display_dialog(self, message: dict) -> DialogExits:
        """
        Displays the dialog based on the message
        """
        logger.info(f"Displaying dialog for {message['messageID']}")
        messageVersion = message.get("messageVersion")
        messageProperties = message.get("dialogProperties")
        if messageProperties.get("timer") is None:
            messageProperties["timer"] = "300"
        infobox = [
            f"Last OS: {self.last_os_version}\n\n"
            f"Current OS: {self.current_os_version}\n\n"
            f"Message Version: {messageVersion}\n\n"
            f"This message will automatically close after {int(messageProperties['timer']) // 60} minutes and will display at next login if not acknowledged."
        ]
        if self.ignore_os_updated or self.test_file or self.test_os_version:
            infobox.append("\n\n**DEBUG MODE**")
        messageProperties["infobox"] = "".join(infobox)

        cmd = [
            "/usr/local/bin/dialog",
            "--jsonstring",
            json.dumps(messageProperties),
        ]
        try:
            dialog = subprocess.run(cmd)
            logger.debug(f"Dialog return code {dialog.returncode}")
            user_messages = Conversion.pythonCollectionFromPropertyList(
                self.get_preference("seenMessages")
            )
            if message.get("alwaysRequired"):
                logger.info(
                    f"Message {message['messageID']} meets the requirements to always display, not setting preference!"
                )
                return DialogExits.ALWAYSREQ
            if not user_messages:
                user_messages = {}
                user_messages[self.current_user] = {}
            user_messages[self.current_user][message["messageID"]] = message["messageVersion"]
            match dialog.returncode:
                case 0:
                    logger.info(f"Displayed dialog for {message['messageID']}")
                    self.set_preference("seenMessages", user_messages)
                    return DialogExits.SUCCESS
                case 4:
                    logger.error(f"Dialog reached timer for {message['messageID']}")
                    return DialogExits.TIMER
                case 10:
                    logger.error(f"Dialog was quit for {message['messageID']}")
                    return DialogExits.QUIT
                case _:
                    logger.error(f"Dialog failed to display for {message['messageID']}")
                    return DialogExits.FAILED

        except subprocess.CalledProcessError:
            logger.error(f"Something broke here {message['messageID']}!")
            return self.DialogExits.CRITFAIL


def get_current_user() -> str:
    """
    Gets the current user
    """
    username = (SCDynamicStoreCopyConsoleUser(None, None, None) or [None])[0]
    username = [username, ""][username in ["loginwindow", None, ""]]
    return username


def main():
    """
    It's the main event
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("user", metavar="user", help="User to run against, used by outset")
    parser.add_argument(
        "optionals",
        metavar="optionals",
        help="Additional options not yet implemented",
        nargs="?",
    )
    parser.add_argument(
        "--testfile",
        help="Local yaml file to skip downloading config",
        default=None,
        type=pathlib.Path,
    )
    parser.add_argument("--testosversion", help="Pass in a test osversion", default=None)
    parser.add_argument(
        "--ignoreupdated",
        help="Pass to ignore if the system is already updated",
        action="store_true",
    )
    parser.add_argument(
        "--cache-dir",
        help="Directory to use as cache dir",
        type=pathlib.Path,
        default=None,
    )
    parser.add_argument("--verbose", "-v", help="Enable DEBUG output", action="store_true")
    args = parser.parse_args()
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    current_user = get_current_user()
    if args.user != current_user:
        logger.error(
            f"Outset user is not the current user! Outset: {args.user}, System: {current_user}",
        )
        sys.exit(1)

    buddy = UpgradeBuddy(
        testosversion=args.testosversion,
        testfile=args.testfile,
        ignoreupdated=args.ignoreupdated,
        cachedir=args.cache_dir,
        user=args.user,
    )
    logger.debug(vars(buddy))
    if not buddy.os_updated:
        logger.info("System did not update, exiting")
        sys.exit(0)
    if not buddy.valid_user:
        logger.critical(
            f"User is in ignore list! User: {args.user}, IgnoredUsers: {buddy.get_ignored_users()}"
        )
        sys.exit(0)
    try:
        buddy.waitfornetwork()
    except TimeoutError:
        sys.exit(1)
    applicable_messages = buddy.process_all_messages()
    logger.debug(applicable_messages)
    message_returns = {}
    for message in applicable_messages:
        message_returns[message["messageID"]] = buddy.display_dialog(message).value
    logger.debug(f"Message Returns: {message_returns}")
    if all(ret < 1 for ret in message_returns.values()):
        if args.ignoreupdated:
            logger.info("Debug mode enabled, not setting last logged os")
            sys.exit(0)
        buddy.set_last_logged_version()
    elif not message_returns:
        logger.info("No messages displayed")
    else:
        logger.error(
            "Messages failed to display all messages properly, not logging OS to trigger run at next login"
        )


if __name__ == "__main__":
    main()
