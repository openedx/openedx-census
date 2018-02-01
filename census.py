"""Visit Open edX sites and count their courses."""

import asyncio
import collections
import csv
import itertools
import logging
import os
import pprint
import re
import time
import traceback
from xml.sax.saxutils import escape

import aiohttp
import async_timeout
import attr
import click
import opaque_keys
import opaque_keys.edx.keys

from html_writer import HtmlOutlineWriter
from site_patterns import find_site_functions

# We don't use anything from this module, it just registers all the parsers.
import sites


log = logging.getLogger(__name__)

@attr.s
class Site:
    url = attr.ib()
    latest_courses = attr.ib()
    current_courses = attr.ib(default=None)
    course_ids = attr.ib(default=attr.Factory(collections.Counter))
    tried = attr.ib(default=attr.Factory(list))
    time = attr.ib(default=None)


GET_KWARGS = dict(verify_ssl=False)

USER_AGENT = "Open edX census-taker. Tell us about your site: oscm+census@edx.org"

class SmartSession:
    def __init__(self):
        headers = {
            'User-Agent': USER_AGENT,
        }
        self.session = aiohttp.ClientSession(headers=headers, raise_for_status=True)
        self.save_numbers = itertools.count()

    async def __aenter__(self):
        await self.session.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.session.__aexit__(exc_type, exc_val, exc_tb)

    def __getattr__(self, name):
        return getattr(self.session, name)

    async def text_from_url(self, url, came_from=None, method='get', save=False):
        headers = {}
        if came_from:
            async with self.session.get(came_from, **GET_KWARGS) as resp:
                real_url = str(resp.url)
                x = await resp.read()
            cookies = self.session.cookie_jar.filter_cookies(url)
            if 'csrftoken' in cookies:
                headers['X-CSRFToken'] = cookies['csrftoken'].value

            headers['Referer'] = real_url

        async with getattr(self.session, method)(url, headers=headers, **GET_KWARGS) as response:
            text = await response.read()

        if save or int(os.environ.get('SAVE', 0)):
            with open("save{}.html".format(next(self.save_numbers)), "wb") as f:
                f.write(text)
        return text

    async def real_url(self, url):
        async with self.session.get(url, **GET_KWARGS) as resp:
            return str(resp.url)


MAX_CLIENTS = 500
TIMEOUT = 20


async def parse_site(site, session, sem):
    async with sem:
        start = time.time()
        with async_timeout.timeout(TIMEOUT):
            for parser in find_site_functions(site.url):
                try:
                    site.current_courses = await parser(site, session)
                except Exception as exc:
                    site.tried.append((parser.__name__, traceback.format_exc()))
                else:
                    site.tried.append((parser.__name__, None))
                    print(".", end='', flush=True)
                    break
            else:
                print("X", end='', flush=True)
        site.time = time.time() - start


async def run(sites):
    tasks = []
    sem = asyncio.Semaphore(MAX_CLIENTS)

    async with SmartSession() as session:
        for site in sites:
            task = asyncio.ensure_future(parse_site(site, session, sem))
            tasks.append(task)

        responses = await asyncio.gather(*tasks)
        print()

def get_urls(sites):
    loop = asyncio.get_event_loop()
    future = asyncio.ensure_future(run(sites))
    # Some exceptions go to stderr and then to my except clause? Shut up.
    loop.set_exception_handler(lambda loop, context: None)
    loop.run_until_complete(future)

def read_sites_file(f):
    next(f)
    for row in csv.reader(f):
        url = row[1].strip().strip("/")
        courses = int(row[2] or 0)
        if not url.startswith("http"):
            url = "http://" + url
        yield url, courses

def read_sites(csv_file, ignore=None):
    ignored = set()
    if ignore:
        with open(ignore) as f:
            for url, _ in read_sites_file(f):
                ignored.add(url)
    with open(csv_file) as f:
        for url, courses in read_sites_file(f):
            if url not in ignored:
                yield Site(url, courses)

@click.command(help=__doc__)
@click.option('--min', type=int, default=1)
@click.option('--format', type=click.Choice(['text', 'html']), default='text')
@click.argument('site_patterns', nargs=-1)
def main(min, format, site_patterns):
    # Make the list of sites we're going to scrape.
    sites = list(read_sites("sites.csv", ignore="ignore.csv"))
    sites = [s for s in sites if s.latest_courses >= min]
    if site_patterns:
        sites = [s for s in sites if any(re.search(p, s.url) for p in site_patterns)]
    print(f"{len(sites)} sites")

    # SCRAPE!
    get_urls(sites)

    # Prep data for reporting.
    sites_descending = sorted(sites, key=lambda s: s.latest_courses, reverse=True)
    old = new = 0
    for site in sites:
        if site.current_courses:
            old += site.latest_courses
            new += site.current_courses

    all_courses = collections.defaultdict(list)
    all_course_ids = set()
    for site in sites:
        for course_id, num in site.course_ids.items():
            all_course_ids.add(course_id)
            try:
                key = opaque_keys.edx.keys.CourseKey.from_string(course_id)
            except opaque_keys.InvalidKeyError:
                course = course_id
            else:
                course = f"{key.org}+{key.course}"
            all_courses[course].append(site)

    with open("course-ids.txt", "w") as f:
        f.write("".join(i + "\n" for i in sorted(all_course_ids)))


    if format == 'text':
        reporter = text_report
    elif format == 'html':
        reporter = html_report
    reporter(sites_descending, old, new, all_courses)


def text_report(sites, old, new, all_courses):
    print(f"Found courses went from {old} to {new}")
    for site in sites:
        print(f"{site.url}: {site.latest_courses} --> {site.current_courses}")
        for strategy, tb in site.tried:
            if tb is not None:
                line = tb.splitlines()[-1]
            else:
                line = "Worked"
            print(f"    {strategy}: {line}")

def html_report(sites, old, new, all_courses):
    with open("sites.html", "w") as htmlout:
        CSS = """\
            html {
                font-family: sans-serif;
            }

            pre {
                font-family: Consolas, monospace;
            }

            .url {
                font-weight: bold;
            }
            .strategy {
                font-style: italic;
            }
        """

        writer = HtmlOutlineWriter(htmlout, css=CSS)
        writer.start_section(f"{len(sites)} sites: {old} &rarr; {new}")
        for site in sites:
            writer.start_section(f"<a class='url' href='{site.url}'>{site.url}</a>: {site.latest_courses} &rarr; {site.current_courses} ({site.time:.1f}s)")
            for strategy, tb in site.tried:
                if tb is not None:
                    line = tb.splitlines()[-1][:100]
                    writer.start_section(f"<span class='strategy'>{strategy}:</span> {escape(line)}")
                    writer.write("""<pre class="stdout">""")
                    writer.write(escape(tb))
                    writer.write("""</pre>""")
                    writer.end_section()
                else:
                    writer.write(f"<p>{strategy}: worked</p>")
            writer.end_section()
        writer.end_section()

        total_course_ids = sum(len(sites) for sites in all_courses.values())
        writer.start_section(f"<p>Course IDs: {total_course_ids}</p>")
        all_courses_items = sorted(all_courses.items(), key=lambda item: len(item[1]), reverse=True)
        for course_id, sites in all_courses_items:
            writer.start_section(f"{course_id}: {len(sites)}")
            for site in sites:
                writer.write(f"<p><a class='url' href='{site.url}'>{site.url}</a></p>")
            writer.end_section()
        writer.end_section()

if __name__ == '__main__':
    main()
