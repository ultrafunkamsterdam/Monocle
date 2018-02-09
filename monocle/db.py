from datetime import datetime
from collections import OrderedDict
from contextlib import contextmanager
from enum import Enum
from time import time, mktime

from sqlalchemy import Column, Integer, String, Float, Boolean, SmallInteger, BigInteger, ForeignKey, UniqueConstraint, create_engine, cast, func, desc, asc, and_, exists
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.types import TypeDecorator, Numeric, Text
from sqlalchemy.ext.declarative import declarative_base

from . import bounds, spawns, db_proc, sanitized as conf
from .utils import time_until_time, dump_pickle, load_pickle
from .shared import call_at, get_logger

try:
    assert conf.LAST_MIGRATION < time()
except AssertionError:
    raise ValueError('LAST_MIGRATION must be a timestamp from the past.')

log = get_logger(__name__)

if conf.DB_ENGINE.startswith('mysql'):
    from sqlalchemy.dialects.mysql import TINYINT, MEDIUMINT, BIGINT, DOUBLE

    TINY_TYPE = TINYINT(unsigned=True)          # 0 to 255
    MEDIUM_TYPE = MEDIUMINT(unsigned=True)      # 0 to 4294967295
    HUGE_TYPE = BIGINT(unsigned=True)           # 0 to 18446744073709551615
    FLOAT_TYPE = DOUBLE(precision=17, scale=14, asdecimal=False)
elif conf.DB_ENGINE.startswith('postgres'):
    from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION

    class NumInt(TypeDecorator):
        '''Modify Numeric type for integers'''
        impl = Numeric

        def process_bind_param(self, value, dialect):
            return int(value)

        def process_result_value(self, value, dialect):
            return int(value)

        @property
        def python_type(self):
            return int

    TINY_TYPE = SmallInteger                    # -32768 to 32767
    MEDIUM_TYPE = Integer                       # -2147483648 to 2147483647
    HUGE_TYPE = NumInt(precision=20, scale=0)   # up to 20 digits
    FLOAT_TYPE = DOUBLE_PRECISION(asdecimal=False)
else:
    class TextInt(TypeDecorator):
        '''Modify Text type for integers'''
        impl = Text

        def process_bind_param(self, value, dialect):
            return str(value)

        def process_result_value(self, value, dialect):
            return int(value)

    TINY_TYPE = SmallInteger
    MEDIUM_TYPE = Integer
    HUGE_TYPE = TextInt
    FLOAT_TYPE = Float(asdecimal=False)

ID_TYPE = BigInteger if conf.SPAWN_ID_INT else String(35)


class Team(Enum):
    none = 0
    mystic = 1
    valor = 2
    instict = 3


def combine_key(sighting):
    return sighting['encounter_id'], sighting['spawn_id']


class SightingCache:
    """Simple cache for storing actual sightings

    It's used in order not to make as many queries to the database.
    It schedules sightings to be removed as soon as they expire.
    """
    def __init__(self):
        self.store = {}

    def __len__(self):
        return len(self.store)

    def add(self, sighting):
        self.store[sighting['spawn_id']] = sighting['expire_timestamp']
        call_at(sighting['expire_timestamp'], self.remove, sighting['spawn_id'])

    def remove(self, spawn_id):
        try:
            del self.store[spawn_id]
        except KeyError:
            pass

    def __contains__(self, raw_sighting):
        try:
            expire_timestamp = self.store[raw_sighting['spawn_id']]
            return (
                expire_timestamp > raw_sighting['expire_timestamp'] - 2 and
                expire_timestamp < raw_sighting['expire_timestamp'] + 2)
        except KeyError:
            return False


class MysteryCache:
    """Simple cache for storing Pokemon with unknown expiration times

    It's used in order not to make as many queries to the database.
    It schedules sightings to be removed an hour after being seen.
    """
    def __init__(self):
        self.store = {}

    def __len__(self):
        return len(self.store)

    def add(self, sighting):
        key = combine_key(sighting)
        self.store[combine_key(sighting)] = [sighting['seen']] * 2
        call_at(sighting['seen'] + 3510, self.remove, key)

    def __contains__(self, raw_sighting):
        key = combine_key(raw_sighting)
        try:
            first, last = self.store[key]
        except (KeyError, TypeError):
            return False
        new_time = raw_sighting['seen']
        if new_time > last:
            self.store[key][1] = new_time
        return True

    def remove(self, key):
        first, last = self.store[key]
        del self.store[key]
        if last != first:
            encounter_id, spawn_id = key
            db_proc.add({
                'type': 'mystery-update',
                'spawn': spawn_id,
                'encounter': encounter_id,
                'first': first,
                'last': last
            })

    def items(self):
        return self.store.items()


class RaidCache:
    """Simple cache for storing actual raids

    It's used in order not to make as many queries to the database.
    It schedules raids to be removed as soon as they expire.
    """
    def __init__(self):
        self.store = {}

    def __len__(self):
        return len(self.store)

    def add(self, raid):
        self.store[raid['fort_external_id']] = raid
        call_at(raid['time_end'], self.remove, raid['fort_external_id'])

    def remove(self, cache_id):
        try:
            del self.store[cache_id]
        except KeyError:
            pass

    def __contains__(self, raw_fort):
        try:
            raid = self.store[raw_fort.id]
            if raw_fort.raid_info.raid_pokemon:
                return (
                    raid['time_end'] > raw_fort.raid_info.raid_end_ms // 1000 - 2 and
                    raid['time_end'] < raw_fort.raid_info.raid_end_ms // 1000 + 2 and
                    raid['pokemon_id'] == raw_fort.raid_info.raid_pokemon.pokemon_id)
            return True
        except KeyError:
            return False

    # Preloading from db
    def preload(self):
        with session_scope() as session:
            raids = session.query(Raid) \
                .filter(Raid.time_end > time())
            for raid in raids:
                fort = session.query(Fort) \
                    .filter(Fort.id == raid.fort_id) \
                    .scalar()
                r = {}
                r['fort_external_id'] = fort.external_id
                r['time_end'] = raid.time_end
                r['pokemon_id'] = raid.pokemon_id
                self.add(r)


class FortCache:
    """Simple cache for storing fort sightings"""
    def __init__(self):
        self.gyms = {}
        self.pokestops = set()
        self.class_version = 2
        self.unpickle()

    def __len__(self):
        return len(self.gyms)

    def add(self, sighting):
        self.gyms[sighting['external_id']] = sighting['last_modified']

    def __contains__(self, sighting):
        try:
            return self.gyms[sighting.id] == sighting.last_modified_timestamp_ms // 1000
        except KeyError:
            return False

    def pickle(self):
        state = self.__dict__.copy()
        state['db_hash'] = spawns.db_hash
        state['bounds_hash'] = hash(bounds)
        dump_pickle('forts', state)

    def unpickle(self):
        try:
            state = load_pickle('forts', raise_exception=True)
            if all((state['class_version'] == self.class_version,
                    state['db_hash'] == spawns.db_hash,
                    state['bounds_hash'] == hash(bounds))):
                self.__dict__.update(state)
        except (FileNotFoundError, TypeError, KeyError):
            pass


class WeatherCache:
    """Simple cache for storing actual weathers

    It's used in order not to make as many queries to the database.
    It schedules raids to be removed as soon as they expire.
    """
    def __init__(self):
        self.store = {}

    def __len__(self):
        return len(self.store)

    def add(self, weather):
        self.store[weather['s2_cell_id']] = weather

    def remove(self, cache_id):
        try:
            del self.store[cache_id]
        except KeyError:
            pass

    def __contains__(self, raw_weather):
        try:
            weather = self.store[raw_weather['s2_cell_id']]
            return (weather['condition'] == raw_weather['condition'] and
                weather['alert_severity'] == raw_weather['alert_severity'] and
                weather['warn'] == raw_weather['warn'] and
                weather['day'] == raw_weather['day'])
        except KeyError:
            return False


SIGHTING_CACHE = SightingCache()
MYSTERY_CACHE = MysteryCache()
FORT_CACHE = FortCache()
RAID_CACHE = RaidCache()
WEATHER_CACHE = WeatherCache()

Base = declarative_base()

_engine = create_engine(conf.DB_ENGINE)
Session = sessionmaker(bind=_engine)
DB_TYPE = _engine.name


if conf.REPORT_SINCE:
    SINCE_TIME = mktime(conf.REPORT_SINCE.timetuple())
    SINCE_QUERY = 'WHERE expire_timestamp > {}'.format(SINCE_TIME)
else:
    SINCE_QUERY = ''


class Sighting(Base):
    __tablename__ = 'sightings'

    id = Column(Integer, primary_key=True)
    pokemon_id = Column(SmallInteger)
    spawn_id = Column(ID_TYPE)
    expire_timestamp = Column(Integer, index=True)
    encounter_id = Column(HUGE_TYPE, index=True)
    lat = Column(FLOAT_TYPE)
    lon = Column(FLOAT_TYPE)
    atk_iv = Column(TINY_TYPE)
    def_iv = Column(TINY_TYPE)
    sta_iv = Column(TINY_TYPE)
    move_1 = Column(SmallInteger)
    move_2 = Column(SmallInteger)
    display = Column(SmallInteger)
    cp = Column(Integer)
    level = Column(SmallInteger)

    __table_args__ = (
        UniqueConstraint(
            'encounter_id',
            'expire_timestamp',
            name='timestamp_encounter_id_unique'
        ),
    )


class Raid(Base):
    __tablename__ = 'raids'

    id = Column(Integer, primary_key=True)
    external_id = Column(ID_TYPE, unique=True)
    fort_id = Column(Integer, ForeignKey('forts.id'))
    level = Column(TINY_TYPE)
    pokemon_id = Column(SmallInteger)
    move_1 = Column(SmallInteger)
    move_2 = Column(SmallInteger)
    time_spawn = Column(Integer, index=True)
    time_battle = Column(Integer)
    time_end = Column(Integer)
    cp = Column(Integer)


class Weather(Base):
    __tablename__ = 'weather'

    id = Column(Integer, primary_key=True)
    s2_cell_id = Column(BigInteger)
    condition = Column(TINY_TYPE)
    alert_severity = Column(TINY_TYPE)
    warn = Column(Boolean)
    day = Column(TINY_TYPE)


class Mystery(Base):
    __tablename__ = 'mystery_sightings'

    id = Column(Integer, primary_key=True)
    pokemon_id = Column(SmallInteger)
    spawn_id = Column(ID_TYPE, index=True)
    encounter_id = Column(HUGE_TYPE, index=True)
    lat = Column(FLOAT_TYPE)
    lon = Column(FLOAT_TYPE)
    first_seen = Column(Integer, index=True)
    first_seconds = Column(SmallInteger)
    last_seconds = Column(SmallInteger)
    seen_range = Column(SmallInteger)
    atk_iv = Column(TINY_TYPE)
    def_iv = Column(TINY_TYPE)
    sta_iv = Column(TINY_TYPE)
    move_1 = Column(SmallInteger)
    move_2 = Column(SmallInteger)
    cp = Column(Integer)
    level = Column(SmallInteger)
    __table_args__ = (
        UniqueConstraint(
            'encounter_id',
            'spawn_id',
            name='unique_encounter'
        ),
    )


class Spawnpoint(Base):
    __tablename__ = 'spawnpoints'

    id = Column(Integer, primary_key=True)
    spawn_id = Column(ID_TYPE, unique=True, index=True)
    despawn_time = Column(SmallInteger, index=True)
    lat = Column(FLOAT_TYPE)
    lon = Column(FLOAT_TYPE)
    updated = Column(Integer, index=True)
    duration = Column(TINY_TYPE)
    failures = Column(TINY_TYPE)


class Fort(Base):
    __tablename__ = 'forts'

    id = Column(Integer, primary_key=True)
    external_id = Column(String(35), unique=True)
    lat = Column(FLOAT_TYPE)
    lon = Column(FLOAT_TYPE)

    sightings = relationship(
        'FortSighting',
        backref='fort',
        order_by='FortSighting.last_modified'
    )

    raids = relationship(
        'Raid',
        backref='fort',
        order_by='Raid.time_end'
    )


class FortSighting(Base):
    __tablename__ = 'fort_sightings'

    id = Column(Integer, primary_key=True)
    fort_id = Column(Integer, ForeignKey('forts.id'))
    last_modified = Column(Integer, index=True)
    team = Column(TINY_TYPE)
    guard_pokemon_id = Column(SmallInteger)
    slots_available = Column(Integer)
    occupied_seconds = Column(Integer)
    total_gym_cp = Column(Integer)
    lowest_pokemon_motivation = Column(Integer)
    name = Column(String(128))
    image_url = Column(String(256))

    __table_args__ = (
        UniqueConstraint(
            'fort_id',
            'last_modified',
            name='fort_id_last_modified_unique'
        ),
    )


class Pokestop(Base):
    __tablename__ = 'pokestops'

    id = Column(Integer, primary_key=True)
    external_id = Column(String(35), unique=True)
    lat = Column(FLOAT_TYPE, index=True)
    lon = Column(FLOAT_TYPE, index=True)
    modifier = Column(Integer)

@contextmanager
def session_scope(autoflush=False):
    """Provide a transactional scope around a series of operations."""
    session = Session(autoflush=autoflush)
    try:
        yield session
        session.commit()
    except:
        session.rollback()
        raise
    finally:
        session.close()


def add_sighting(session, pokemon):
    # Check if there isn't the same entry already
    if pokemon in SIGHTING_CACHE:
        return
    if session.query(exists().where(and_(
                Sighting.expire_timestamp == pokemon['expire_timestamp'],
                Sighting.encounter_id == pokemon['encounter_id']))
            ).scalar():
        SIGHTING_CACHE.add(pokemon)
        return
    obj = Sighting(
        pokemon_id=pokemon['pokemon_id'],
        spawn_id=pokemon['spawn_id'],
        encounter_id=pokemon['encounter_id'],
        expire_timestamp=pokemon['expire_timestamp'],
        lat=pokemon['lat'],
        lon=pokemon['lon'],
        atk_iv=pokemon.get('individual_attack'),
        def_iv=pokemon.get('individual_defense'),
        sta_iv=pokemon.get('individual_stamina'),
        move_1=pokemon.get('move_1'),
        move_2=pokemon.get('move_2'),
        display=pokemon.get('display')
    )
    session.add(obj)
    SIGHTING_CACHE.add(pokemon)


def add_spawnpoint(session, pokemon):
    # Check if the same entry already exists
    spawn_id = pokemon['spawn_id']
    new_time = pokemon['expire_timestamp'] % 3600
    try:
        if new_time == spawns.despawn_times[spawn_id]:
            return
    except KeyError:
        pass
    existing = session.query(Spawnpoint) \
        .filter(Spawnpoint.spawn_id == spawn_id) \
        .first()
    now = round(time())
    point = pokemon['lat'], pokemon['lon']
    spawns.add_known(spawn_id, new_time, point)
    if existing:
        existing.updated = now
        existing.failures = 0

        if (existing.despawn_time is None or
                existing.updated < conf.LAST_MIGRATION):
            widest = get_widest_range(session, spawn_id)
            if widest and widest > 1800:
                existing.duration = 60
        elif new_time == existing.despawn_time:
            return

        existing.despawn_time = new_time
    else:
        widest = get_widest_range(session, spawn_id)

        duration = 60 if widest and widest > 1800 else None

        session.add(Spawnpoint(
            spawn_id=spawn_id,
            despawn_time=new_time,
            lat=pokemon['lat'],
            lon=pokemon['lon'],
            updated=now,
            duration=duration,
            failures=0
        ))


def add_mystery_spawnpoint(session, pokemon):
    # Check if the same entry already exists
    spawn_id = pokemon['spawn_id']
    point = pokemon['lat'], pokemon['lon']
    if point in spawns.unknown or session.query(exists().where(
            Spawnpoint.spawn_id == spawn_id)).scalar():
        return

    session.add(Spawnpoint(
        spawn_id=spawn_id,
        despawn_time=None,
        lat=pokemon['lat'],
        lon=pokemon['lon'],
        updated=0,
        duration=None,
        failures=0
    ))

    if point in bounds:
        spawns.add_unknown(point)


def add_mystery(session, pokemon):
    if pokemon in MYSTERY_CACHE:
        return
    add_mystery_spawnpoint(session, pokemon)
    existing = session.query(Mystery) \
        .filter(Mystery.encounter_id == pokemon['encounter_id']) \
        .filter(Mystery.spawn_id == pokemon['spawn_id']) \
        .first()
    if existing:
        key = combine_key(pokemon)
        MYSTERY_CACHE.store[key] = [existing.first_seen, pokemon['seen']]
        return
    seconds = pokemon['seen'] % 3600
    obj = Mystery(
        pokemon_id=pokemon['pokemon_id'],
        spawn_id=pokemon['spawn_id'],
        encounter_id=pokemon['encounter_id'],
        lat=pokemon['lat'],
        lon=pokemon['lon'],
        first_seen=pokemon['seen'],
        first_seconds=seconds,
        last_seconds=seconds,
        seen_range=0,
        atk_iv=pokemon.get('individual_attack'),
        def_iv=pokemon.get('individual_defense'),
        sta_iv=pokemon.get('individual_stamina'),
        move_1=pokemon.get('move_1'),
        move_2=pokemon.get('move_2')
    )
    session.add(obj)
    MYSTERY_CACHE.add(pokemon)


def add_fort_sighting(session, raw_fort):
    # Check if fort exists
    fort = session.query(Fort) \
        .filter(Fort.external_id == raw_fort['external_id']) \
        .first()
    if not fort:
        fort = Fort(
            external_id=raw_fort['external_id'],
            lat=raw_fort['lat'],
            lon=raw_fort['lon'],
        )
        session.add(fort)
    if fort.id and session.query(exists().where(and_(
                FortSighting.fort_id == fort.id,
                FortSighting.last_modified == raw_fort['last_modified']
            ))).scalar():
        # Why is it not in the cache? It should be there!
        FORT_CACHE.add(raw_fort)
        return

    obj = FortSighting(
        fort=fort,
        team=raw_fort['team'],
        guard_pokemon_id=raw_fort['guard_pokemon_id'],
        last_modified=raw_fort['last_modified'],
        slots_available=raw_fort['slots_available'],
        occupied_seconds=raw_fort['occupied_seconds'],
        total_gym_cp=raw_fort['total_gym_cp'],
        lowest_pokemon_motivation=raw_fort['lowest_pokemon_motivation'],
        name=raw_fort['name'],
        image_url=raw_fort['image_url']

    )
    session.add(obj)
    FORT_CACHE.add(raw_fort)


def add_raid(session, raw_raid):
    fort = session.query(Fort) \
        .filter(Fort.external_id == raw_raid['fort_external_id']) \
        .first()
    if not fort:
        fort = Fort(
            external_id=raw_raid['fort_external_id'],
            lat=raw_raid['lat'],
            lon=raw_raid['lon'],
        )
        session.add(fort)

    raid = session.query(Raid) \
        .filter(Raid.external_id == raw_raid['external_id']) \
        .first()
    if fort.id and raid:
        if raid.pokemon_id == 0 and raw_raid['pokemon_id'] != 0:
            raid.pokemon_id = raw_raid['pokemon_id']
            raid.move_1 = raw_raid['move_1']
            raid.move_2 = raw_raid['move_2']
            raid.cp = raw_raid['cp']
        # Why is it not in the cache? It should be there!
        RAID_CACHE.add(raw_raid)
        return

    raid = Raid(
        external_id=raw_raid['external_id'],
        fort=fort,
        level=raw_raid['level'],
        pokemon_id=raw_raid['pokemon_id'],
        move_1=raw_raid['move_1'],
        move_2=raw_raid['move_2'],
        cp=raw_raid['cp'],
        time_spawn=raw_raid['time_spawn'],
        time_battle=raw_raid['time_battle'],
        time_end=raw_raid['time_end']
    )
    session.add(raid)
    RAID_CACHE.add(raw_raid)


def add_pokestop(session, raw_pokestop):
    pokestop_id = raw_pokestop['external_id']
    modifier = raw_pokestop['modifier']
    
    pokestop = session.query(Pokestop) \
            .filter(Pokestop.external_id == pokestop_id) \
            .first()
            
    if pokestop is None:
        pokestop = Pokestop(
            external_id=pokestop_id,
            lat=raw_pokestop['lat'],
            lon=raw_pokestop['lon'],
            modifier=raw_pokestop['modifier'],
        )
        session.add(pokestop)
        FORT_CACHE.pokestops.add(pokestop_id)
        
    else:
        if pokestop.modifier != modifier:
            pokestop.modifier = modifier
            session.commit()
            return
    

def add_weather(session, raw_weather):
    s2_cell_id = raw_weather['s2_cell_id']

    weather = session.query(Weather) \
        .filter(Weather.s2_cell_id == s2_cell_id) \
        .first()
    if not weather:
        weather = Weather(
            s2_cell_id=s2_cell_id,
            condition=raw_weather['condition'],
            alert_severity=raw_weather['alert_severity'],
            warn=raw_weather['warn'],
            day=raw_weather['day']
        )
        session.add(weather)
    else:
        weather.condition = raw_weather['condition']
        weather.alert_severity = raw_weather['alert_severity']
        weather.warn = raw_weather['warn']
        weather.day = raw_weather['day']
    WEATHER_CACHE.add(raw_weather)


def update_failures(session, spawn_id, success, allowed=conf.FAILURES_ALLOWED):
    spawnpoint = session.query(Spawnpoint) \
        .filter(Spawnpoint.spawn_id == spawn_id) \
        .first()
    if not hasattr(spawnpoint, 'failures'):
        pass
    try:
        if success:
            spawnpoint.failures = 0
        elif spawnpoint.failures >= allowed:
            if spawnpoint.duration == 60:
                spawnpoint.duration = None
                log.warning('{} consecutive failures on {}, no longer treating as an hour spawn.', allowed + 1, spawn_id)
            else:
                spawnpoint.updated = 0
                try:
                    del spawns.despawn_times[spawn_id]
                except KeyError:
                    pass
                log.warning('{} consecutive failures on {}, will treat as an unknown from now on.', allowed + 1, spawn_id)
            spawnpoint.failures = 0
        else:
            spawnpoint.failures += 1
    except TypeError:
        spawnpoint.failures = 1


def update_mystery(session, mystery):
    encounter = session.query(Mystery) \
                .filter(Mystery.spawn_id == mystery['spawn']) \
                .filter(Mystery.encounter_id == mystery['encounter']) \
                .first()
    if not encounter:
        return
    hour = encounter.first_seen - (encounter.first_seen % 3600)
    encounter.last_seconds = mystery['last'] - hour
    encounter.seen_range = mystery['last'] - mystery['first']


def get_pokestops(session):
    return session.query(Pokestop).all()


def _get_forts_sqlite(session):
    # SQLite version is sloooooow compared to MySQL
    return session.execute('''
        SELECT
            fs.fort_id,
            fs.id,
            fs.team,
            fs.prestige,
            fs.guard_pokemon_id,
            fs.last_modified,
            f.lat,
            f.lon,
            fs.slots_available
        FROM fort_sightings fs
        JOIN forts f ON f.id=fs.fort_id
        WHERE fs.fort_id || '-' || fs.last_modified IN (
            SELECT fort_id || '-' || MAX(last_modified)
            FROM fort_sightings
            GROUP BY fort_id
        )
    ''').fetchall()


def _get_forts(session):
    return session.execute('''
SELECT
fs.fort_id,
fs.id,
fs.team,
fs.guard_pokemon_id,
fs.last_modified,
f.lat,
f.lon,
fs.slots_available,
fs.image_url,
fs.name,
fs.occupied_seconds
FROM fort_sightings fs
JOIN forts f ON f.id=fs.fort_id
WHERE (fs.fort_id, fs.last_modified) IN (SELECT fort_id, MAX(last_modified) FROM fort_sightings
GROUP BY fort_id)
    ''').fetchall()

get_forts = _get_forts_sqlite if DB_TYPE == 'sqlite' else _get_forts


def get_session_stats(session):
    query = session.query(func.min(Sighting.expire_timestamp),
        func.max(Sighting.expire_timestamp))
    if conf.REPORT_SINCE:
        query = query.filter(Sighting.expire_timestamp > SINCE_TIME)
    min_max_result = query.one()
    length_hours = (min_max_result[1] - min_max_result[0]) // 3600
    if length_hours == 0:
        length_hours = 1
    # Convert to datetime
    return {
        'start': datetime.fromtimestamp(min_max_result[0]),
        'end': datetime.fromtimestamp(min_max_result[1]),
        'length_hours': length_hours
    }


def get_first_last(session, spawn_id):
    return session.query(func.min(Mystery.first_seconds), func.max(Mystery.last_seconds)) \
        .filter(Mystery.spawn_id == spawn_id) \
        .filter(Mystery.first_seen > conf.LAST_MIGRATION) \
        .first()


def get_widest_range(session, spawn_id):
    return session.query(func.max(Mystery.seen_range)) \
        .filter(Mystery.spawn_id == spawn_id) \
        .filter(Mystery.first_seen > conf.LAST_MIGRATION) \
        .scalar()


def estimate_remaining_time(session, spawn_id, seen):
    first, last = get_first_last(session, spawn_id)

    if not first:
        return 90, 1800

    if seen > last:
        last = seen
    elif seen < first:
        first = seen

    if last - first > 1710:
        estimates = [
            time_until_time(x, seen)
            for x in (first + 90, last + 90, first + 1800, last + 1800)]
        return min(estimates), max(estimates)

    soonest = last + 90
    latest = first + 1800
    return time_until_time(soonest, seen), time_until_time(latest, seen)


def get_punch_card(session):
    query = session.query(cast(Sighting.expire_timestamp / 300, Integer).label('ts_date'), func.count('ts_date')) \
        .group_by('ts_date') \
        .order_by('ts_date')
    if conf.REPORT_SINCE:
        query = query.filter(Sighting.expire_timestamp > SINCE_TIME)
    results = query.all()
    results_dict = {r[0]: r[1] for r in results}
    filled = []
    for row_no, i in enumerate(range(int(results[0][0]), int(results[-1][0]))):
        filled.append((row_no, results_dict.get(i, 0)))
    return filled


def get_top_pokemon(session, count=30, order='DESC'):
    query = session.query(Sighting.pokemon_id, func.count(Sighting.pokemon_id).label('how_many')) \
        .group_by(Sighting.pokemon_id)
    if conf.REPORT_SINCE:
        query = query.filter(Sighting.expire_timestamp > SINCE_TIME)
    order = desc if order == 'DESC' else asc
    query = query.order_by(order('how_many')).limit(count)
    return query.all()


def get_pokemon_ranking(session):
    query = session.query(Sighting.pokemon_id, func.count(Sighting.pokemon_id).label('how_many')) \
        .group_by(Sighting.pokemon_id) \
        .order_by(asc('how_many'))
    if conf.REPORT_SINCE:
        query = query.filter(Sighting.expire_timestamp > SINCE_TIME)
    ranked = [r[0] for r in query]
    none_seen = [x for x in range(1,387) if x not in ranked]
    return none_seen + ranked


def get_sightings_per_pokemon(session):
    query = session.query(Sighting.pokemon_id, func.count(Sighting.pokemon_id).label('how_many')) \
        .group_by(Sighting.pokemon_id) \
        .order_by('how_many')
    if conf.REPORT_SINCE:
        query = query.filter(Sighting.expire_timestamp > SINCE_TIME)
    return OrderedDict(query.all())


def sightings_to_csv(since=None, output='sightings.csv'):
    from csv import writer as csv_writer

    if since:
        conf.REPORT_SINCE = since
    with session_scope() as session:
        sightings = get_sightings_per_pokemon(session)
    od = OrderedDict()
    for pokemon_id in range(1, 387):
        if pokemon_id not in sightings:
            od[pokemon_id] = 0
    od.update(sightings)
    with open(output, 'wt') as csvfile:
        writer = csv_writer(csvfile)
        writer.writerow(('pokemon_id', 'count'))
        for item in od.items():
            writer.writerow(item)


def get_rare_pokemon(session):
    result = []

    for pokemon_id in conf.RARE_IDS:
        query = session.query(Sighting) \
            .filter(Sighting.pokemon_id == pokemon_id)
        if conf.REPORT_SINCE:
            query = query.filter(Sighting.expire_timestamp > SINCE_TIME)
        count = query.count()
        if count > 0:
            result.append((pokemon_id, count))
    return result


def get_nonexistent_pokemon(session):
    query = session.execute('''
        SELECT DISTINCT pokemon_id FROM sightings
        {report_since}
    '''.format(report_since=SINCE_QUERY))
    db_ids = [r[0] for r in query]
    return [x for x in range(1,387) if x not in db_ids]


def get_all_sightings(session, pokemon_ids):
    # TODO: rename this and get_sightings
    query = session.query(Sighting) \
        .filter(Sighting.pokemon_id.in_(pokemon_ids))
    if conf.REPORT_SINCE:
        query = query.filter(Sighting.expire_timestamp > SINCE_TIME)
    return query.all()


def get_spawns_per_hour(session, pokemon_id):
    if DB_TYPE == 'sqlite':
        ts_hour = 'STRFTIME("%H", expire_timestamp)'
    elif DB_TYPE == 'postgresql':
        ts_hour = "TO_CHAR(TO_TIMESTAMP(expire_timestamp), 'HH24')"
    else:
        ts_hour = 'HOUR(FROM_UNIXTIME(expire_timestamp))'
    query = session.execute('''
        SELECT
            {ts_hour} AS ts_hour,
            COUNT(*) AS how_many
        FROM sightings
        WHERE pokemon_id = {pokemon_id}
        {report_since}
        GROUP BY ts_hour
        ORDER BY ts_hour
    '''.format(
        pokemon_id=pokemon_id,
        ts_hour=ts_hour,
        report_since=SINCE_QUERY.replace('WHERE', 'AND')
    ))
    results = []
    for result in query:
        results.append((
            {
                'v': [int(result[0]), 30, 0],
                'f': '{}:00 - {}:00'.format(
                    int(result[0]), int(result[0]) + 1
                ),
            },
            result[1]
        ))
    return results


def get_total_spawns_count(session, pokemon_id):
    query = session.query(Sighting) \
        .filter(Sighting.pokemon_id == pokemon_id)
    if conf.REPORT_SINCE:
        query = query.filter(Sighting.expire_timestamp > SINCE_TIME)
    return query.count()


def get_all_spawn_coords(session, pokemon_id=None):
    points = session.query(Sighting.lat, Sighting.lon)
    if pokemon_id:
        points = points.filter(Sighting.pokemon_id == int(pokemon_id))
    if conf.REPORT_SINCE:
        points = points.filter(Sighting.expire_timestamp > SINCE_TIME)
    return points.all()
