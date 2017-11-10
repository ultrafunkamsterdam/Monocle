from asyncio import gather, Lock, Semaphore, sleep, CancelledError
from collections import deque
from time import time, monotonic
from queue import Empty
from itertools import cycle
from sys import exit
from distutils.version import StrictVersion

from aiopogo import PGoApi, HashServer, json_loads, exceptions as ex
from aiopogo.auth_ptc import AuthPtc
from cyrandom import choice, randint, uniform
from pogeo import get_distance

from .db import FORT_CACHE, MYSTERY_CACHE, SIGHTING_CACHE, RAID_CACHE
from .utils import round_coords, load_pickle, get_device_info, get_start_coords, Units, randomize_point
from .shared import get_logger, LOOP, SessionManager, run_threaded, ACCOUNTS
from . import altitudes, avatar, bounds, db_proc, spawns, sanitized as conf

if conf.NOTIFY or conf.NOTIFY_RAIDS:
    from .notification import Notifier

if conf.CACHE_CELLS:
    from array import typecodes
    if 'Q' in typecodes:
        from pogeo import get_cell_ids_compact as _pogeo_cell_ids
    else:
        from pogeo import get_cell_ids as _pogeo_cell_ids
else:
    from pogeo import get_cell_ids as _pogeo_cell_ids


_unit = getattr(Units, conf.SPEED_UNIT.lower())
if conf.SPIN_POKESTOPS:
    if _unit is Units.miles:
        SPINNING_SPEED_LIMIT = 21
        UNIT_STRING = "MPH"
    elif _unit is Units.kilometers:
        SPINNING_SPEED_LIMIT = 34
        UNIT_STRING = "KMH"
    elif _unit is Units.meters:
        SPINNING_SPEED_LIMIT = 34000
        UNIT_STRING = "m/h"
UNIT = _unit.value
del _unit


class Worker:
    """Single worker walking on the map"""

    download_hash = ''
    scan_delay = conf.SCAN_DELAY if conf.SCAN_DELAY >= 10 else 10
    g = {'seen': 0, 'captchas': 0}

    if conf.CACHE_CELLS:
        cells = load_pickle('cells') or {}

        @classmethod
        def get_cell_ids(cls, point):
            rounded = round_coords(point, 4)
            try:
                return cls.cells[rounded]
            except KeyError:
                cells = _pogeo_cell_ids(rounded)
                cls.cells[rounded] = cells
                return cells
    else:
        get_cell_ids = _pogeo_cell_ids

    login_semaphore = Semaphore(conf.SIMULTANEOUS_LOGINS, loop=LOOP)
    sim_semaphore = Semaphore(conf.SIMULTANEOUS_SIMULATION, loop=LOOP)

    multiproxy = False
    if conf.PROXIES:
        if len(conf.PROXIES) > 1:
            multiproxy = True
        proxies = cycle(conf.PROXIES)
    else:
        proxies = None

    if conf.NOTIFY or conf.NOTIFY_RAIDS:
        notifier = Notifier()

    def __init__(self, worker_no):
        self.worker_no = worker_no
        self.log = get_logger('worker-{}'.format(worker_no))
        # account information
        try:
            self.account = self.extra_queue.get_nowait()
        except Empty as e:
            try:
                self.account = self.captcha_queue.get_nowait()
            except Empty as e:
                raise ValueError("You don't have enough accounts for the number of workers specified in GRID.") from e
        self.username = self.account['username']
        try:
            self.location = self.account['location'][:2]
        except KeyError:
            self.location = get_start_coords(worker_no)
        self.altitude = None
        # last time of any request
        self.last_request = self.account.get('time', 0)
        # last time of a request that requires user interaction in the game
        self.last_action = self.last_request
        # last time of a GetMapObjects request
        self.last_gmo = self.last_request
        try:
            self.items = self.account['items']
            self.bag_items = sum(self.items.values())
        except KeyError:
            self.account['items'] = {}
            self.items = self.account['items']
        self.inventory_timestamp = self.account.get('inventory_timestamp', 0) if self.items else 0
        self.player_level = self.account.get('level')
        self.num_captchas = 0
        self.eggs = {}
        self.unused_incubators = deque()
        self.initialize_api()
        # State variables
        self.busy = Lock(loop=LOOP)
        # Other variables
        self.after_spawn = 0
        self.speed = 0
        self.total_seen = 0
        self.error_code = 'INIT'
        self.item_capacity = 350
        self.visits = 0
        self.pokestops = conf.SPIN_POKESTOPS
        self.next_spin = 0
        self.handle = HandleStub()

    def initialize_api(self):
        device_info = get_device_info(self.account)
        self.empty_visits = 0

        self.api = PGoApi(device_info=device_info)
        self.api.set_position(*self.location, self.altitude)
        if self.proxies:
            self.api.proxy = next(self.proxies)
        try:
            if self.account['provider'] == 'ptc' and 'auth' in self.account:
                self.api.auth_provider = AuthPtc(username=self.username, password=self.account['password'], timeout=conf.LOGIN_TIMEOUT)
                self.api.auth_provider._access_token = self.account['auth']
                self.api.auth_provider._access_token_expiry = self.account['expiry']
                if self.api.auth_provider.check_access_token():
                    self.api.auth_provider.authenticated = True
        except KeyError:
            pass

    def swap_proxy(self):
        proxy = self.api.proxy
        while proxy == self.api.proxy:
            self.api.proxy = next(self.proxies)

    async def login(self, reauth=False):
        """Logs worker in and prepares for scanning"""
        self.log.info('Trying to log in')

        for attempt in range(-1, conf.MAX_RETRIES):
            try:
                self.error_code = '»'
                async with self.login_semaphore:
                    self.error_code = 'LOGIN'
                    await self.api.set_authentication(
                        username=self.username,
                        password=self.account['password'],
                        provider=self.account.get('provider') or 'ptc',
                        timeout=conf.LOGIN_TIMEOUT
                    )
            except ex.UnexpectedAuthError as e:
                await self.swap_account('unexpected auth error')
            except ex.AuthException as e:
                err = e
                await sleep(2, loop=LOOP)
            else:
                err = None
                break
        if reauth:
            if err:
                self.error_code = 'NOT AUTHENTICATED'
                self.log.info('Re-auth error on {}: {}', self.username, err)
                return False
            self.error_code = None
            return True
        if err:
            raise err

        self.error_code = '°'
        version = 7904
        async with self.sim_semaphore:
            self.error_code = 'APP SIMULATION'
            if conf.APP_SIMULATION:
                await self.app_simulation_login(version)
            else:
                await self.download_remote_config(version)

        self.error_code = None
        return True

    async def get_player(self):
        request = self.api.create_request()
        request.get_player(player_locale=conf.PLAYER_LOCALE)

        responses = await self.call(request, chain=False)

        tutorial_state = None
        try:
            get_player = responses['GET_PLAYER']

            if get_player.warn:
                raise ex.WarnAccountException
            if get_player.banned:
                raise ex.BannedAccountException

            player_data = get_player.player_data
            tutorial_state = player_data.tutorial_state
            # API can return 0 as capacity.
            if player_data.max_item_storage != 0:
                self.item_capacity = player_data.max_item_storage
            if 'created' not in self.account:
                self.account['created'] = player_data.creation_timestamp_ms / 1000
        except (KeyError, TypeError, AttributeError):
            pass
        return tutorial_state

    async def download_remote_config(self, version):
        request = self.api.create_request()
        request.download_remote_config_version(platform=1, app_version=version)
        responses = await self.call(request, buddy=False, settings=True, inbox=False, dl_hash=False)

        try:
            inventory_items = responses['GET_INVENTORY'].inventory_delta.inventory_items
            for item in inventory_items:
                level = item.inventory_item_data.player_stats.level
                if level:
                    self.player_level = level
                    break
        except KeyError:
            pass

        await self.random_sleep(.78, 1.05)
        try:
            remote_config = responses['DOWNLOAD_REMOTE_CONFIG_VERSION']
            return (
                remote_config.asset_digest_timestamp_ms / 1000000,
                remote_config.item_templates_timestamp_ms / 1000)
        except KeyError:
            return 0.0, 0.0

    async def set_avatar(self, tutorial=False):
        plater_avatar = avatar.new()
        request = self.api.create_request()
        request.list_avatar_customizations(
            avatar_type=plater_avatar['avatar'],
            slot=tuple(),
            filters=(2,)
        )
        await self.call(request, buddy=not tutorial, inbox=False, action=5)
        await self.random_sleep(7, 14)

        request = self.api.create_request()
        request.set_avatar(player_avatar=plater_avatar)
        await self.call(request, buddy=not tutorial, inbox=False, action=2)

        if tutorial:
            await self.random_sleep(.5, 4)

            request = self.api.create_request()
            request.mark_tutorial_complete(tutorials_completed=(1,))
            await self.call(request, buddy=False, inbox=False)

        await self.random_sleep(.5, 1)

        request = self.api.create_request()
        request.get_player_profile()
        await self.call(request, inbox=False, action=1)

    async def app_simulation_login(self, version):
        self.log.info('Starting RPC login sequence (iOS app simulation)')

        # empty request
        request = self.api.create_request()
        await self.call(request, chain=False)
        await self.random_sleep(.43, .97)

        # request 1: get_player
        tutorial_state = await self.get_player()
        await self.random_sleep(.53, 1.1)

        # request 2: download_remote_config_version
        asset_time, template_time = await self.download_remote_config(version)

        if asset_time > self.account.get('asset_time', 0.0):
            # request 3: get_asset_digest
            i = randint(0, 3)
            result = 2
            page_offset = 0
            page_timestamp = 0
            while result == 2:
                request = self.api.create_request()
                request.get_asset_digest(
                    platform=1,
                    app_version=version,
                    paginate=True,
                    page_offset=page_offset,
                    page_timestamp=page_timestamp)
                responses = await self.call(request, buddy=False, settings=True, inbox=False)
                if i > 2:
                    await sleep(1.45)
                    i = 0
                else:
                    i += 1
                    await sleep(.2)
                try:
                    response = responses['GET_ASSET_DIGEST']
                except KeyError:
                    break
                result = response.result
                page_offset = response.page_offset
                page_timestamp = response.timestamp_ms
            self.account['asset_time'] = asset_time

        if template_time > self.account.get('template_time', 0.0):
            # request 4: download_item_templates
            i = randint(0, 3)
            result = 2
            page_offset = 0
            page_timestamp = 0
            while result == 2:
                request = self.api.create_request()
                request.download_item_templates(
                    paginate=True,
                    page_offset=page_offset,
                    page_timestamp=page_timestamp)
                responses = await self.call(request, buddy=False, settings=True, inbox=False)
                if i > 2:
                    await sleep(1.5)
                    i = 0
                else:
                    i += 1
                    await sleep(.25)
                try:
                    response = responses['DOWNLOAD_ITEM_TEMPLATES']
                except KeyError:
                    break
                result = response.result
                page_offset = response.page_offset
                page_timestamp = response.timestamp_ms

            self.account['template_time'] = template_time

        if (conf.COMPLETE_TUTORIAL and
                tutorial_state is not None and
                not all(x in tutorial_state for x in (0, 1, 3, 4, 7))):
            self.log.warning('{} is starting tutorial', self.username)
            await self.complete_tutorial(tutorial_state)
        else:
            # request 5: get_player_profile
            request = self.api.create_request()
            request.get_player_profile()
            await self.call(request, settings=True, inbox=False)
            await self.random_sleep(.2, .3)

            if self.player_level:
                # request 6: level_up_rewards
                request = self.api.create_request()
                request.level_up_rewards(level=self.player_level)
                await self.call(request, settings=True)
                await self.random_sleep(.45, .7)
            else:
                self.log.warning('No player level')

            request = self.api.create_request()
            request.get_store_items()
            await self.call(request, chain=False)
            await self.random_sleep(.43, .97)

            self.log.info('Finished RPC login sequence (iOS app simulation)')
            await self.random_sleep(.5, 1.3)
        self.error_code = None
        return True

    async def complete_tutorial(self, tutorial_state):
        self.error_code = 'TUTORIAL'
        if 0 not in tutorial_state:
            # legal screen
            request = self.api.create_request()
            request.mark_tutorial_complete(tutorials_completed=(0,))
            await self.call(request, buddy=False, inbox=False)

            await self.random_sleep(.35, .525)

            request = self.api.create_request()
            request.get_player(player_locale=conf.PLAYER_LOCALE)
            await self.call(request, buddy=False, inbox=False)
            await sleep(1)

        if 1 not in tutorial_state:
            # avatar selection
            await self.set_avatar(tutorial=True)

        starter_id = None
        if 3 not in tutorial_state:
            # encounter tutorial
            await self.random_sleep(.7, .9)
            request = self.api.create_request()
            request.get_download_urls(asset_id=
                ('1a3c2816-65fa-4b97-90eb-0b301c064b7a/1487275569649000',
                'aa8f7687-a022-4773-b900-3a8c170e9aea/1487275581132582',
                'e89109b0-9a54-40fe-8431-12f7826c8194/1487275593635524'))
            await self.call(request, inbox=False)

            await self.random_sleep(7, 10.3)
            request = self.api.create_request()
            starter = choice((1, 4, 7))
            request.encounter_tutorial_complete(pokemon_id=starter)
            responses = await self.call(request, inbox=False, action=1)

            try:
                inventory = responses['GET_INVENTORY'].inventory_delta.inventory_items
                for item in inventory:
                    pokemon = item.inventory_item_data.pokemon_data
                    if pokemon.id:
                        starter_id = pokemon.id
                        break
            except (KeyError, TypeError):
                starter_id = None

            await self.random_sleep(.4, .5)
            request = self.api.create_request()
            request.get_player(player_locale=conf.PLAYER_LOCALE)
            await self.call(request, inbox=False)

        if 4 not in tutorial_state:
            # name selection
            await self.random_sleep(12, 18)
            request = self.api.create_request()
            request.claim_codename(codename=self.username)
            await self.call(request, inbox=False, action=2)

            await sleep(.7, loop=LOOP)
            request = self.api.create_request()
            request.get_player(player_locale=conf.PLAYER_LOCALE)
            await self.call(request, inbox=False)
            await sleep(.13, loop=LOOP)

            request = self.api.create_request()
            request.mark_tutorial_complete(tutorials_completed=(4,))
            await self.call(request, inbox=False)

        if 7 not in tutorial_state:
            # first time experience
            await self.random_sleep(3.9, 4.5)
            request = self.api.create_request()
            request.mark_tutorial_complete(tutorials_completed=(7,))
            await self.call(request, inbox=False)

        if starter_id:
            await self.random_sleep(4, 5)
            request = self.api.create_request()
            request.set_buddy_pokemon(pokemon_id=starter_id)
            await self.call(request, inbox=False, action=2)
            await self.random_sleep(.8, 1.2)

        await sleep(.2, loop=LOOP)
        return True

    def update_inventory(self, inventory_items):
        for thing in inventory_items:
            obj = thing.inventory_item_data
            if obj.HasField('item'):
                item = obj.item
                self.items[item.item_id] = item.count
                self.bag_items = sum(self.items.values())
            elif conf.INCUBATE_EGGS:
                if obj.HasField('pokemon_data') and obj.pokemon_data.is_egg:
                    egg = obj.pokemon_data
                    self.eggs[egg.id] = egg
                elif obj.HasField('egg_incubators'):
                    self.unused_incubators.clear()
                    for item in obj.egg_incubators.egg_incubator:
                        if item.pokemon_id:
                            continue
                        if item.item_id == 901:
                            self.unused_incubators.append(item)
                        else:
                            self.unused_incubators.appendleft(item)

    async def call(self, request, chain=True, buddy=True, settings=False, inbox=True, dl_hash=True, action=None):
        if chain:
            request.check_challenge()
            request.get_hatched_eggs()
            request.get_inventory(last_timestamp_ms=self.inventory_timestamp)
            request.check_awarded_badges()
            if settings:
                if dl_hash:
                    request.download_settings(hash=self.download_hash)
                else:
                    request.download_settings()
            if buddy:
                request.get_buddy_walked()
            if inbox:
                request.get_inbox(is_history=True)

        if action:
            now = time()
            # wait for the time required, or at least a half-second
            if self.last_action > now + .5:
                await sleep(self.last_action - now, loop=LOOP)
            else:
                await sleep(0.5, loop=LOOP)

        response = None
        err = None
        for attempt in range(-1, conf.MAX_RETRIES):
            try:
                responses = await request.call()
                self.last_request = time()
                err = None
                break
            except (ex.NotLoggedInException, ex.AuthException) as e:
                self.log.info('Auth error on {}: {}', self.username, e)
                err = e
                await sleep(3, loop=LOOP)
                if not await self.login(reauth=True):
                    await self.swap_account(reason='reauth failed')
            except ex.TimeoutException as e:
                self.error_code = 'TIMEOUT'
                if not isinstance(e, type(err)):
                    err = e
                    self.log.warning('{}', e)
                await sleep(10, loop=LOOP)
            except ex.HashingOfflineException as e:
                if not isinstance(e, type(err)):
                    err = e
                    self.log.warning('{}', e)
                self.error_code = 'HASHING OFFLINE'
                await sleep(5, loop=LOOP)
            except ex.NianticOfflineException as e:
                if not isinstance(e, type(err)):
                    err = e
                    self.log.warning('{}', e)
                self.error_code = 'NIANTIC OFFLINE'
                await self.random_sleep()
            except ex.HashingQuotaExceededException as e:
                if not isinstance(e, type(err)):
                    err = e
                    self.log.warning('Exceeded your hashing quota, sleeping.')
                self.error_code = 'QUOTA EXCEEDED'
                refresh = HashServer.status.get('period')
                now = time()
                if refresh:
                    if refresh > now:
                        await sleep(refresh - now + 1, loop=LOOP)
                    else:
                        await sleep(5, loop=LOOP)
                else:
                    await sleep(30, loop=LOOP)
            except ex.BadRPCException:
                raise
            except ex.InvalidRPCException as e:
                self.last_request = time()
                if not isinstance(e, type(err)):
                    err = e
                    self.log.warning('{}', e)
                self.error_code = 'INVALID REQUEST'
                await self.random_sleep()
            except ex.ProxyException as e:
                if not isinstance(e, type(err)):
                    err = e
                self.error_code = 'PROXY ERROR'

                if self.multiproxy:
                    self.log.error('{}, swapping proxy.', e)
                    self.swap_proxy()
                else:
                    if not isinstance(e, type(err)):
                        self.log.error('{}', e)
                    await sleep(5, loop=LOOP)
            except (ex.MalformedResponseException, ex.UnexpectedResponseException) as e:
                self.last_request = time()
                if not isinstance(e, type(err)):
                    self.log.warning('{}', e)
                self.error_code = 'MALFORMED RESPONSE'
                await self.random_sleep()
        if err is not None:
            raise err

        if action:
            # pad for time that action would require
            self.last_action = self.last_request + action

        try:
            delta = responses['GET_INVENTORY'].inventory_delta
            self.inventory_timestamp = delta.new_timestamp_ms
            self.update_inventory(delta.inventory_items)
        except KeyError:
            pass

        if settings:
            try:
                dl_settings = responses['DOWNLOAD_SETTINGS']
                Worker.download_hash = dl_settings.hash
            except KeyError:
                self.log.info('Missing DOWNLOAD_SETTINGS response.')
            else:
                if (not dl_hash
                        and conf.FORCED_KILL
                        and dl_settings.settings.minimum_client_version != '0.79.4'):
                    forced_version = StrictVersion(dl_settings.settings.minimum_client_version)
                    if forced_version > StrictVersion('0.79.4'):
                        err = '{} is being forced, exiting.'.format(forced_version)
                        self.log.error(err)
                        print(err)
                        exit()
        try:
            challenge_url = responses['CHECK_CHALLENGE'].challenge_url
            if challenge_url != ' ':
                self.g['captchas'] += 1
                if conf.CAPTCHA_KEY:
                    self.log.warning('{} has encountered a CAPTCHA, trying to solve', self.username)
                    await self.handle_captcha(challenge_url)
                else:
                    raise CaptchaException
        except KeyError:
            pass
        return responses

    def travel_speed(self, point):
        '''Fast calculation of travel speed to point'''
        time_diff = max(time() - self.last_request, self.scan_delay)
        distance = get_distance(self.location, point, UNIT)
        # conversion from seconds to hours
        speed = (distance / time_diff) * 3600
        return speed

    async def bootstrap_visit(self, point):
        for _ in range(3):
            if await self.visit(point, bootstrap=True):
                return True
            self.error_code = '∞'
            self.simulate_jitter(0.00005)
        return False

    async def visit(self, point, spawn_id=None, bootstrap=False):
        """Wrapper for self.visit_point - runs it a few times before giving up

        Also is capable of restarting in case an error occurs.
        """
        try:
            try:
                self.altitude = altitudes.get(point)
            except KeyError:
                self.altitude = await altitudes.fetch(point)
            self.location = point
            self.api.set_position(*self.location, self.altitude)
            if not self.authenticated:
                await self.login()
            return await self.visit_point(point, spawn_id, bootstrap)
        except ex.NotLoggedInException:
            self.error_code = 'NOT AUTHENTICATED'
            await sleep(1, loop=LOOP)
            if not await self.login(reauth=True):
                await self.swap_account(reason='reauth failed')
            return await self.visit(point, spawn_id, bootstrap)
        except ex.AuthException as e:
            self.log.warning('Auth error on {}: {}', self.username, e)
            self.error_code = 'NOT AUTHENTICATED'
            await sleep(3, loop=LOOP)
            await self.swap_account(reason='login failed')
        except CaptchaException:
            self.error_code = 'CAPTCHA'
            self.g['captchas'] += 1
            await sleep(1, loop=LOOP)
            await self.bench_account()
        except CaptchaSolveException:
            self.error_code = 'CAPTCHA'
            await sleep(1, loop=LOOP)
            await self.swap_account(reason='solving CAPTCHA failed')
        except ex.TempHashingBanException:
            self.error_code = 'HASHING BAN'
            self.log.error('Temporarily banned from hashing server for using invalid keys.')
            await sleep(185, loop=LOOP)
        except ex.WarnAccountException:
            self.error_code = 'WARN'
            self.log.warning('{} is warn', self.username)
            await sleep(1, loop=LOOP)
            await self.remove_account(warn=True)
        except ex.BannedAccountException:
            self.error_code = 'BANNED'
            self.log.warning('{} is banned', self.username)
            await sleep(1, loop=LOOP)
            await self.remove_account()
        except ex.ProxyException as e:
            self.error_code = 'PROXY ERROR'

            if self.multiproxy:
                self.log.error('{} Swapping proxy.', e)
                self.swap_proxy()
            else:
                self.log.error('{}', e)
        except ex.TimeoutException as e:
            self.log.warning('{} Giving up.', e)
        except ex.NianticIPBannedException:
            self.error_code = 'IP BANNED'

            if self.multiproxy:
                self.log.warning('Swapping out {} due to IP ban.', self.api.proxy)
                self.swap_proxy()
            else:
                self.log.error('IP banned.')
        except ex.NianticOfflineException as e:
            await self.swap_account(reason='Niantic endpoint failure')
            self.log.warning('{}. Giving up.', e)
        except ex.ServerBusyOrOfflineException as e:
            self.log.warning('{} Giving up.', e)
        except ex.BadRPCException:
            self.error_code = 'BAD REQUEST'
            self.log.warning('{} received code 3 and is likely banned. Removing until next run.', self.username)
            await self.new_account()
        except ex.InvalidRPCException as e:
            self.log.warning('{} Giving up.', e)
        except ex.ExpiredHashKeyException as e:
            self.error_code = 'KEY EXPIRED'
            err = str(e)
            self.log.error(err)
            print(err)
            exit()
        except (ex.MalformedResponseException, ex.UnexpectedResponseException) as e:
            self.log.warning('{} Giving up.', e)
            self.error_code = 'MALFORMED RESPONSE'
        except EmptyGMOException as e:
            self.error_code = '0'
            self.log.warning('Empty GetMapObjects response for {}. Speed: {:.2f}', self.username, self.speed)
        except ex.HashServerException as e:
            self.log.warning('{}', e)
            self.error_code = 'HASHING ERROR'
        except ex.AiopogoError as e:
            self.log.exception(e.__class__.__name__)
            self.error_code = 'AIOPOGO ERROR'
        except CancelledError:
            self.log.warning('Visit cancelled.')
        except Exception as e:
            self.log.exception('A wild {} appeared!', e.__class__.__name__)
            self.error_code = 'EXCEPTION'
        return False

    async def visit_point(self, point, spawn_id, bootstrap,
            encounter_conf=conf.ENCOUNTER, notify_conf=conf.NOTIFY,
            more_points=conf.MORE_POINTS):
        self.handle.cancel()
        self.error_code = '∞' if bootstrap else '!'

        self.log.info('Visiting {0[0]:.4f},{0[1]:.4f}', point)
        start = time()

        cell_ids = self.get_cell_ids(point)
        since_timestamp_ms = (0,) * len(cell_ids)
        request = self.api.create_request()
        request.get_map_objects(cell_id=cell_ids,
                                since_timestamp_ms=since_timestamp_ms,
                                latitude=point[0],
                                longitude=point[1])

        diff = self.last_gmo + self.scan_delay - time()
        if diff > 0:
            await sleep(diff, loop=LOOP)
        responses = await self.call(request)
        self.last_gmo = self.last_request

        try:
            map_objects = responses['GET_MAP_OBJECTS']

            if map_objects.status != 1:
                error = 'GetMapObjects code for {}. Speed: {:.2f}'.format(self.username, self.speed)
                self.empty_visits += 1
                if self.empty_visits > 3:
                    reason = '{} empty visits'.format(self.empty_visits)
                    await self.swap_account(reason)
                raise ex.UnexpectedResponseException(error)
        except KeyError:
            await self.random_sleep(.5, 1)
            await self.get_player()
            raise ex.UnexpectedResponseException('Missing GetMapObjects response.')

        pokemon_seen = 0
        forts_seen = 0
        points_seen = 0
        seen_target = not spawn_id

        if conf.ITEM_LIMITS and self.bag_items >= self.item_capacity:
            await self.clean_bag()

        for map_cell in map_objects.map_cells:
            request_time_ms = map_cell.current_timestamp_ms
            for pokemon in map_cell.wild_pokemons:
                pokemon_seen += 1

                normalized = self.normalize_pokemon(pokemon)
                seen_target = seen_target or normalized['spawn_id'] == spawn_id

                if (normalized not in SIGHTING_CACHE and
                        normalized not in MYSTERY_CACHE):
                    if (encounter_conf == 'all'
                            or (encounter_conf == 'some'
                            and normalized['pokemon_id'] in conf.ENCOUNTER_IDS)):
                        try:
                            await self.encounter(normalized, pokemon.spawn_point_id)
                        except CancelledError:
                            db_proc.add(normalized)
                            raise
                        except Exception as e:
                            self.log.warning('{} during encounter', e.__class__.__name__)

                if notify_conf and self.notifier.eligible(normalized):
                    if encounter_conf and 'move_1' not in normalized:
                        try:
                            await self.encounter(normalized, pokemon.spawn_point_id)
                        except CancelledError:
                            db_proc.add(normalized)
                            raise
                        except Exception as e:
                            self.log.warning('{} during encounter', e.__class__.__name__)
                    LOOP.create_task(self.notifier.notify(normalized, map_objects.time_of_day))
                db_proc.add(normalized)

            for fort in map_cell.forts:
                if not fort.enabled:
                    continue
                forts_seen += 1
                if fort.type == 1:  # pokestops
                    if fort.HasField('lure_info'):
                        norm = self.normalize_lured(fort, request_time_ms)
                        pokemon_seen += 1
                        if norm not in SIGHTING_CACHE:
                            db_proc.add(norm)
                    if (self.pokestops and
                            self.bag_items < self.item_capacity
                            and time() > self.next_spin
                            and (not conf.SMART_THROTTLE or
                            self.smart_throttle(2))):
                        cooldown = fort.cooldown_complete_timestamp_ms
                        if not cooldown or time() > cooldown / 1000:
                            await self.spin_pokestop(fort)
                    if fort.id not in FORT_CACHE.pokestops:
                        pokestop = self.normalize_pokestop(fort)
                        db_proc.add(pokestop)
                else:
                    if fort not in FORT_CACHE:
                        db_proc.add(self.normalize_gym(fort))
                    if fort.HasField('raid_info'):
                        if fort not in RAID_CACHE:
                            if conf.NOTIFY_RAIDS:
                                LOOP.create_task(self.notifier.notify_raid(fort))
                            raid = self.normalize_raid(fort)
                            db_proc.add(raid)

            if more_points:
                try:
                    for p in map_cell.spawn_points:
                        points_seen += 1
                        p = p.latitude, p.longitude
                        if spawns.have_point(p) or p not in bounds:
                            continue
                        spawns.cell_points.add(p)
                except KeyError:
                    pass

        if spawn_id:
            db_proc.add({
                'type': 'target',
                'seen': seen_target,
                'spawn_id': spawn_id})

        if (conf.INCUBATE_EGGS and self.unused_incubators
                and self.eggs and self.smart_throttle()):
            await self.incubate_eggs()

        if pokemon_seen > 0:
            self.error_code = ':'
            self.total_seen += pokemon_seen
            self.g['seen'] += pokemon_seen
            self.empty_visits = 0
        else:
            self.empty_visits += 1
            if forts_seen == 0:
                self.log.warning('Nothing seen by {}. Speed: {:.2f}', self.username, self.speed)
                self.error_code = '0 SEEN'
            else:
                self.error_code = ','
            if self.empty_visits > 3 and not bootstrap:
                reason = '{} empty visits'.format(self.empty_visits)
                await self.swap_account(reason)
        self.visits += 1

        if conf.MAP_WORKERS:
            self.worker_dict.update([(self.worker_no,
                (point, start, self.speed, self.total_seen,
                self.visits, pokemon_seen))])
        self.log.info(
            'Point processed, {} Pokemon and {} forts seen!',
            pokemon_seen,
            forts_seen,
        )

        self.update_accounts_dict()
        self.handle = LOOP.call_later(60, self.unset_code)
        return pokemon_seen + forts_seen + points_seen

    def smart_throttle(self, requests=1):
        try:
            # https://en.wikipedia.org/wiki/Linear_equation#Two_variables
            # e.g. hashes_left > 2.25*seconds_left+7.5, spare = 0.05, max = 150
            spare = conf.SMART_THROTTLE * HashServer.status['maximum']
            hashes_left = HashServer.status['remaining'] - requests
            usable_per_second = (HashServer.status['maximum'] - spare) / 60
            seconds_left = HashServer.status['period'] - time()
            return hashes_left > usable_per_second * seconds_left + spare
        except (TypeError, KeyError):
            return False

    async def spin_pokestop(self, pokestop):
        self.error_code = '$'
        pokestop_location = pokestop.latitude, pokestop.longitude
        distance = get_distance(self.location, pokestop_location)
        # permitted interaction distance - 4 (for some jitter leeway)
        # estimation of spinning speed limit
        if distance > 36 or self.speed > SPINNING_SPEED_LIMIT:
            self.error_code = '!'
            return False

        # randomize location up to ~1.5 meters
        self.simulate_jitter(amount=0.00001)

        request = self.api.create_request()
        request.fort_details(fort_id = pokestop.id,
                             latitude = pokestop_location[0],
                             longitude = pokestop_location[1])
        responses = await self.call(request, action=1.2)
        name = responses['FORT_DETAILS'].name

        request = self.api.create_request()
        request.fort_search(fort_id = pokestop.id,
                            player_latitude = self.location[0],
                            player_longitude = self.location[1],
                            fort_latitude = pokestop_location[0],
                            fort_longitude = pokestop_location[1])
        responses = await self.call(request, action=2)

        try:
            result = responses['FORT_SEARCH'].result
        except KeyError:
            self.log.warning('Invalid Pokéstop spinning response.')
            self.error_code = '!'
            return

        if result == 1:
            self.log.info('Spun {}.', name)
            try:
                inventory_items = responses['GET_INVENTORY'].inventory_delta.inventory_items
                for item in inventory_items:
                    level = item.inventory_item_data.player_stats.level
                    if level and level > self.player_level:
                        # level_up_rewards if level has changed
                        request = self.api.create_request()
                        request.level_up_rewards(level=level)
                        await self.call(request)
                        self.log.info('Level up, get rewards.', name)
                        self.player_level = level
                        break
            except KeyError:
                pass
        elif result == 2:
            self.log.info('The server said {} was out of spinning range. {:.1f}m {:.1f}{}',
                name, distance, self.speed, UNIT_STRING)
        elif result == 3:
            self.log.warning('{} was in the cooldown period.', name)
        elif result == 4:
            self.log.warning('Could not spin {} because inventory was full. {}',
                name, self.bag_items)
            self.inventory_timestamp = 0
        elif result == 5:
            self.log.warning('Could not spin {} because the daily limit was reached.', name)
            self.pokestops = False
        else:
            self.log.warning('Failed spinning {}: {}', name, result)

        self.next_spin = time() + conf.SPIN_COOLDOWN
        self.error_code = '!'

    async def encounter(self, pokemon, spawn_id):
        distance_to_pokemon = get_distance(self.location, (pokemon['lat'], pokemon['lon']))

        self.error_code = '~'

        if distance_to_pokemon > 48:
            percent = 1 - (47 / distance_to_pokemon)
            lat_change = (self.location[0] - pokemon['lat']) * percent
            lon_change = (self.location[1] - pokemon['lon']) * percent
            self.location = (
                self.location[0] - lat_change,
                self.location[1] - lon_change)
            self.altitude = uniform(self.altitude - 2, self.altitude + 2)
            self.api.set_position(*self.location, self.altitude)
            delay_required = min((distance_to_pokemon * percent) / 8, 1.1)
        else:
            self.simulate_jitter()
            delay_required = 1.1

        await self.random_sleep(delay_required, delay_required + 1.5)

        request = self.api.create_request()
        request = request.encounter(encounter_id=pokemon['encounter_id'],
                                    spawn_point_id=spawn_id,
                                    player_latitude=self.location[0],
                                    player_longitude=self.location[1])

        responses = await self.call(request, action=2.25)

        try:
            pdata = responses['ENCOUNTER'].wild_pokemon.pokemon_data
            pokemon['move_1'] = pdata.move_1
            pokemon['move_2'] = pdata.move_2
            pokemon['individual_attack'] = pdata.individual_attack
            pokemon['individual_defense'] = pdata.individual_defense
            pokemon['individual_stamina'] = pdata.individual_stamina
            pokemon['height'] = pdata.height_m
            pokemon['weight'] = pdata.weight_kg
            pokemon['gender'] = pdata.pokemon_display.gender
        except KeyError:
            self.log.error('Missing encounter response.')
        self.error_code = '!'

    async def clean_bag(self):
        self.error_code = '|'
        rec_items = {}
        limits = conf.ITEM_LIMITS
        for item, count in self.items.items():
            if item in limits and count > limits[item]:
                discard = count - limits[item]
                if discard > 50:
                    rec_items[item] = randint(50, discard)
                else:
                    rec_items[item] = discard

        removed = 0
        for item, count in rec_items.items():
            request = self.api.create_request()
            request.recycle_inventory_item(item_id=item, count=count)
            responses = await self.call(request, action=2)

            try:
                if responses['RECYCLE_INVENTORY_ITEM'].result != 1:
                    self.log.warning("Failed to remove item {}", item)
                else:
                    removed += count
            except KeyError:
                self.log.warning("Failed to remove item {}", item)
        self.log.info("Removed {} items", removed)
        self.error_code = '!'

    async def incubate_eggs(self):
        # copy the deque, as self.call could modify it as it updates the inventory
        incubators = self.unused_incubators.copy()
        for egg in sorted(self.eggs.values(), key=lambda x: x.egg_km_walked_target):
            if not incubators:
                break

            if egg.egg_incubator_id:
                continue

            inc = incubators.pop()
            if inc.item_id == 901 or egg.egg_km_walked_target > 9:
                request = self.api.create_request()
                request.use_item_egg_incubator(item_id=inc.id, pokemon_id=egg.id)
                responses = await self.call(request, action=4.5)

                try:
                    ret = responses['USE_ITEM_EGG_INCUBATOR'].result
                    if ret == 4:
                        self.log.warning("Failed to use incubator because it was already in use.")
                    elif ret != 1:
                        self.log.warning("Failed to apply incubator {} on {}, code: {}",
                            inc.id, egg.id, ret)
                except (KeyError, AttributeError):
                    self.log.error('Invalid response to USE_ITEM_EGG_INCUBATOR')

        self.unused_incubators = incubators

    async def handle_captcha(self, challenge_url):
        if self.num_captchas >= conf.CAPTCHAS_ALLOWED:
            self.log.error("{} encountered too many CAPTCHAs, removing.", self.username)
            raise CaptchaException

        self.error_code = 'C'
        self.num_captchas += 1

        session = SessionManager.get()
        try:
            params = {
                'key': conf.CAPTCHA_KEY,
                'method': 'userrecaptcha',
                'googlekey': '6LeeTScTAAAAADqvhqVMhPpr_vB9D364Ia-1dSgK',
                'pageurl': challenge_url,
                'json': 1
            }
            async with session.post('http://2captcha.com/in.php', params=params) as resp:
                response = await resp.json(loads=json_loads)
        except CancelledError:
            raise
        except Exception as e:
            self.log.error('Got an error while trying to solve CAPTCHA. '
                           'Check your API Key and account balance.')
            raise CaptchaSolveException from e

        code = response.get('request')
        if response.get('status') != 1:
            if code in ('ERROR_WRONG_USER_KEY', 'ERROR_KEY_DOES_NOT_EXIST', 'ERROR_ZERO_BALANCE'):
                conf.CAPTCHA_KEY = None
                self.log.error('2Captcha reported: {}, disabling CAPTCHA solving', code)
            else:
                self.log.error("Failed to submit CAPTCHA for solving: {}", code)
            raise CaptchaSolveException

        try:
            # Get the response, retry every 5 seconds if it's not ready
            params = {
                'key': conf.CAPTCHA_KEY,
                'action': 'get',
                'id': code,
                'json': 1
            }
            while True:
                async with session.get("http://2captcha.com/res.php", params=params, timeout=20) as resp:
                    response = await resp.json(loads=json_loads)
                if response.get('request') != 'CAPCHA_NOT_READY':
                    break
                await sleep(5, loop=LOOP)
        except CancelledError:
            raise
        except Exception as e:
            self.log.error('Got an error while trying to solve CAPTCHA. '
                              'Check your API Key and account balance.')
            raise CaptchaSolveException from e

        token = response.get('request')
        if not response.get('status') == 1:
            self.log.error("Failed to get CAPTCHA response: {}", token)
            raise CaptchaSolveException

        request = self.api.create_request()
        request.verify_challenge(token=token)
        await self.call(request, action=4)
        self.update_accounts_dict()
        self.log.warning("Successfully solved CAPTCHA")

    def simulate_jitter(self, amount=0.00002):
        '''Slightly randomize location, by up to ~3 meters by default.'''
        self.location = randomize_point(self.location)
        self.altitude = uniform(self.altitude - 1, self.altitude + 1)
        self.api.set_position(*self.location, self.altitude)

    def update_accounts_dict(self):
        self.account['location'] = self.location
        self.account['time'] = self.last_request
        self.account['inventory_timestamp'] = self.inventory_timestamp
        if self.player_level:
            self.account['level'] = self.player_level

        try:
            self.account['auth'] = self.api.auth_provider._access_token
            self.account['expiry'] = self.api.auth_provider._access_token_expiry
        except AttributeError:
            pass

        ACCOUNTS[self.username] = self.account

    async def remove_account(self, warn=False):
        self.error_code = 'REMOVING'
        if warn:
            self.account['warn'] = True
            self.log.warning('Removing {} due to warn.', self.username)
        else:
            self.account['banned'] = True
            self.log.warning('Removing {} due to ban.', self.username)
        self.update_accounts_dict()
        await self.new_account()

    async def bench_account(self):
        self.error_code = 'BENCHING'
        self.log.warning('Swapping {} due to CAPTCHA.', self.username)
        self.account['captcha'] = True
        self.update_accounts_dict()
        self.captcha_queue.put(self.account)
        await self.new_account()

    async def lock_and_swap(self, minutes):
        async with self.busy:
            self.error_code = 'SWAPPING'
            h, m = divmod(int(minutes), 60)
            if h:
                timestr = '{}h{}m'.format(h, m)
            else:
                timestr = '{}m'.format(m)
            self.log.warning('Swapping {} which had been running for {}.', self.username, timestr)
            self.update_accounts_dict()
            self.extra_queue.put(self.account)
            await self.new_account()

    async def swap_account(self, reason=''):
        self.error_code = 'SWAPPING'
        self.log.warning('Swapping out {} because {}.', self.username, reason)
        self.update_accounts_dict()
        self.extra_queue.put(self.account)
        await self.new_account()

    async def new_account(self):
        if (conf.CAPTCHA_KEY
                and (conf.FAVOR_CAPTCHA or self.extra_queue.empty())
                and not self.captcha_queue.empty()):
            self.account = self.captcha_queue.get()
        else:
            try:
                self.account = self.extra_queue.get_nowait()
            except Empty:
                self.account = await run_threaded(self.extra_queue.get)
        self.username = self.account['username']
        try:
            self.location = self.account['location'][:2]
        except KeyError:
            self.location = get_start_coords(self.worker_no)
        self.inventory_timestamp = self.account.get('inventory_timestamp', 0) if self.items else 0
        self.player_level = self.account.get('level')
        self.last_request = self.account.get('time', 0)
        self.last_action = self.last_request
        self.last_gmo = self.last_request
        try:
            self.items = self.account['items']
            self.bag_items = sum(self.items.values())
        except KeyError:
            self.account['items'] = {}
            self.items = self.account['items']
        self.num_captchas = 0
        self.eggs = {}
        self.unused_incubators = deque()
        self.initialize_api()
        self.error_code = None

    def unset_code(self):
        self.error_code = None

    @staticmethod
    def normalize_pokemon(raw, spawn_int=conf.SPAWN_ID_INT):
        """Normalizes data coming from API into something acceptable by db"""
        tsm = raw.last_modified_timestamp_ms
        tss = round(tsm / 1000)
        tth = raw.time_till_hidden_ms
        norm = {
            'type': 'pokemon',
            'encounter_id': raw.encounter_id,
            'pokemon_id': raw.pokemon_data.pokemon_id,
            'lat': raw.latitude,
            'lon': raw.longitude,
            'spawn_id': int(raw.spawn_point_id, 16) if spawn_int else raw.spawn_point_id,
            'seen': tss
        }
        if tth > 0 and tth <= 90000:
            norm['expire_timestamp'] = round((tsm + tth) / 1000)
            norm['time_till_hidden'] = tth / 1000
            norm['inferred'] = False
        else:
            despawn = spawns.get_despawn_time(norm['spawn_id'], tss)
            if despawn:
                norm['expire_timestamp'] = despawn
                norm['time_till_hidden'] = despawn - tss
                norm['inferred'] = True
            else:
                norm['type'] = 'mystery'
        if raw.pokemon_data.pokemon_display:
            if raw.pokemon_data.pokemon_display.form:
                norm['display'] = raw.pokemon_data.pokemon_display.form
        return norm

    @staticmethod
    def normalize_lured(raw, now):
        lure = raw.lure_info
        return {
            'type': 'pokemon',
            'encounter_id': lure.encounter_id,
            'pokemon_id': lure.active_pokemon_id,
            'expire_timestamp': lure.lure_expires_timestamp_ms // 1000,
            'lat': raw.latitude,
            'lon': raw.longitude,
            'spawn_id': 0 if conf.SPAWN_ID_INT else 'LURED',
            'time_till_hidden': (lure.lure_expires_timestamp_ms - now) / 1000,
            'inferred': 'pokestop'
        }

    @staticmethod
    def normalize_gym(raw):
        return {
            'type': 'fort',
            'external_id': raw.id,
            'lat': raw.latitude,
            'lon': raw.longitude,
            'team': raw.owned_by_team,
            'prestige': raw.gym_points,
            'guard_pokemon_id': raw.guard_pokemon_id,
            'last_modified': raw.last_modified_timestamp_ms // 1000,
            'slots_available': raw.gym_display.slots_available
        }

    @staticmethod
    def normalize_raid(raw):
        return {
            'type': 'raid',
            'external_id': raw.raid_info.raid_seed,
            'fort_external_id': raw.id,
            'lat': raw.latitude,
            'lon': raw.longitude,
            'level': raw.raid_info.raid_level,
            'pokemon_id': raw.raid_info.raid_pokemon.pokemon_id if raw.raid_info.raid_pokemon else 0,
            'move_1': raw.raid_info.raid_pokemon.move_1 if raw.raid_info.raid_pokemon else 0,
            'move_2': raw.raid_info.raid_pokemon.move_2 if raw.raid_info.raid_pokemon else 0,
            'time_spawn': raw.raid_info.raid_spawn_ms // 1000,
            'time_battle': raw.raid_info.raid_battle_ms // 1000,
            'time_end': raw.raid_info.raid_end_ms // 1000
        }

    @staticmethod
    def normalize_pokestop(raw):
        return {
            'type': 'pokestop',
            'external_id': raw.id,
            'lat': raw.latitude,
            'lon': raw.longitude
        }

    @staticmethod
    async def random_sleep(minimum=10.1, maximum=14, loop=LOOP):
        """Sleeps for a bit"""
        await sleep(uniform(minimum, maximum), loop=loop)

    @property
    def start_time(self):
        return self.api.start_time

    @property
    def status(self):
        """Returns status message to be displayed in status screen"""
        if self.error_code:
            msg = self.error_code
        else:
            msg = 'P{seen}'.format(
                seen=self.total_seen
            )
        return '[W{worker_no}: {msg}]'.format(
            worker_no=self.worker_no,
            msg=msg
        )

    @property
    def authenticated(self):
        try:
            return self.api.auth_provider.authenticated
        except AttributeError:
            return False


class HandleStub:
    def cancel(self):
        pass


class EmptyGMOException(Exception):
    """Raised when the GMO response is empty."""


class CaptchaException(Exception):
    """Raised when a CAPTCHA is needed."""


class CaptchaSolveException(Exception):
    """Raised when solving a CAPTCHA has failed."""
