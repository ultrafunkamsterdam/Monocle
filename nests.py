#!/usr/bin/env python3

from datetime import datetime
from pkg_resources import resource_filename

try:
    from ujson import dumps
    from flask import json as flask_json
    flask_json.dumps = lambda obj, **kwargs: dumps(obj, double_precision=6)
except ImportError:
    from json import dumps

from flask import Flask, jsonify, Markup, render_template, request

from monocle import db, sanitized as conf
from monocle.web_utils import *
from monocle.bounds import area, center

from shapely.geometry import Polygon, Point, LineString
import s2sphere

app = Flask(__name__, template_folder=resource_filename('monocle', 'templates'), static_folder=resource_filename('monocle', 'static'))

def social_links():
    social_links = ''

    if conf.FB_PAGE_ID:
        social_links = '<a class="map_btn facebook-icon" target="_blank" href="https://www.facebook.com/' + conf.FB_PAGE_ID + '"></a>'
    if conf.TWITTER_SCREEN_NAME:
        social_links += '<a class="map_btn twitter-icon" target="_blank" href="https://www.twitter.com/' + conf.TWITTER_SCREEN_NAME + '"></a>'
    if conf.DISCORD_INVITE_ID:
        social_links += '<a class="map_btn discord-icon" target="_blank" href="https://discord.gg/' + conf.DISCORD_INVITE_ID + '"></a>'
    if conf.TELEGRAM_USERNAME:
        social_links += '<a class="map_btn telegram-icon" target="_blank" href="https://www.telegram.me/' + conf.TELEGRAM_USERNAME + '"></a>'

    return Markup(social_links)

def render_nests():
    template = app.jinja_env.get_template('nests.html')
    return template.render(
        area_name=conf.AREA_NAME,
        map_center=center,
        map_provider_url=conf.MAP_PROVIDER_URL,
        map_provider_attribution=conf.MAP_PROVIDER_ATTRIBUTION,
        social_links=social_links()
    )

def get_spawns_at_point():
    with db.session_scope() as session:
        spawns = db.get_sightings_per_spawn(session)
    return dict(spawns)

@app.route('/')
def nest_map(nests_html=render_nests()):
    return nests_html

@app.route('/nest_spawns')
def nest_spawns():
    getPokes = request.args.get('pokes')
    spawns = get_nest_points()
    if getPokes != None:
        try:
            nests = load_pickle('nests_full', raise_exception=True)
            spawns = nests
        except (FileNotFoundError, TypeError, KeyError):
            sightings_per_spawn = get_spawns_at_point()
            for s in spawns:
                sorted = sightings_per_spawn[s['spawn_id']]
                total = sum([p[1] for p in sorted])
                if total == 0:
                    total = 1
                try:
                    for p in sorted:
                        if p[0] in conf.NON_NESTING_IDS:
                            del sorted[sorted.index(p)]
                except:
                    pass
                try:
                    s['pokemon_id'] = sorted[-1][0]
                    s['name'] = POKEMON[s['pokemon_id']] + " {:.2f}".format(sorted[-1][1]/total)
                    # Put together a set of alternative possible nesting species
                    # to display in marker popup.
                    s['alternatives'] = ''
                    try:
                        for p in reversed(sorted[-3:-1]):
                            s['alternatives'] += '<br>' + POKEMON[p[0]] + " {:.2f}".format(p[1]/total)
                    except:
                        try:
                            s['alternatives'] += '<br>' + POKEMON[sorted[-2][0]] + " {:.2f}".format(sorted[-2][1]/total)
                        except:
                            s['alternatives'] += '<br> None'
                except:
                    pass
            dump_pickle('nests_full',spawns)

    return jsonify(spawns)

@app.route('/parks')
def parks():
    return jsonify(get_all_parks())

# Speculation that nest migration is related to L12 cells, so include overlay for interest
@app.route('/L12cells')
def L12cells():
    return jsonify(get_s2_cells(level=12))

@app.route('/scan_coords')
def scan_coords():
    return jsonify(get_scan_coords())

def main():
    args = get_args()
    app.run(debug=args.debug, threaded=True, host=args.host, port=args.port)

if __name__ == '__main__':
    main()
