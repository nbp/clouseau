# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import six
import operator
import calendar
from datetime import (datetime, date, timedelta)
import math
import random
import dateutil.parser
import pytz


__pacific = pytz.timezone('US/Pacific')


def get_best(stats):
    """Get the key which has the higher value

    Args:
        stats (dict): the stats contained in a dictionary

    Returns:
        a key
    """
    if stats:
        return max(stats.items(), key=operator.itemgetter(1))[0]
    else:
        return None


def get_timestamp(dt):
    """Get a timestamp from a date

    Args:
        dt: a string or a datetime object

    Returns:
        int: the corresponding timestamp
    """
    if isinstance(dt, six.string_types):
        dt = get_date_ymd(dt)
    return int(calendar.timegm(dt.timetuple()))


def get_date_ymd(dt):
    """Get a datetime from a string 'Year-month-day'

    Args:
        dt (str): a date

    Returns:
        datetime: a datetime object
    """
    assert dt

    if isinstance(dt, datetime):
        return dt

    if dt == 'today':
        today = date.today()
        return datetime(today.year, today.month, today.day)
    elif dt == 'yesterday':
        yesterday = date.today() - timedelta(1)
        return datetime(yesterday.year, yesterday.month, yesterday.day)
    elif dt == 'tomorrow':
        tomorrow = date.today() + timedelta(1)
        return datetime(tomorrow.year, tomorrow.month, tomorrow.day)
    elif dt == 'today_utc':
        today = datetime.utcnow()
        return datetime(today.year, today.month, today.day)
    elif dt == 'yesterday_utc':
        yesterday = datetime.utcnow() - timedelta(1)
        return datetime(yesterday.year, yesterday.month, yesterday.day)

    return dateutil.parser.parse(dt)


def get_today():
    """Get the date for today

    Returns:
        str: the date of today
    """
    return get_date_str(date.today())


def get_date_str(ymd):
    """Get the date as string

    Args:
        ymd (datetime): a datetime
    Returns:
        str: the date as a string 'Year-month-day'
    """
    return ymd.strftime('%Y-%m-%d')


def get_date(_date, delta=None):
    """Get the date as string

    Args:
        ymd (str): a date
    Returns:
        str: the date as a string 'Year-month-day'
    """
    if isinstance(_date, six.string_types):
        _date = get_date_ymd(_date)
    if delta:
        _date -= timedelta(delta)
    return get_date_str(_date)


def get_now_timestamp():
    """Get timestamp for now

    Returns:
        int: timestamp for now
    """
    return get_timestamp(datetime.utcnow())


def is64(cpu_name):
    """Check if a cpu is 64 bits or not

    Args:
        cpu_name (str): the cpu name

    Returns:
        bool: True if 64 is in the name
    """
    return '64' in cpu_name


def percent(x):
    """Get a percent from a ratio (0.23 => 23%)

    Args:
        x (float): ratio

    Returns:
        str: a string with a percent
    """
    return simple_percent(round(100 * x, 1))


def simple_percent(x):
    """Get a percent string

    Args:
        x (float): number

    Returns:
        str: a string with a percent
    """
    if math.floor(x) == x:
        x = int(x)
    return str(x) + '%'


def get_sample(data, fraction):
    """Get a random sample from the data according to the fraction

    Args:
        data (list): data
        fraction (float): the fraction

    Returns:
        list: a random sample
    """
    if fraction < 0 or fraction >= 1:
        return data
    else:
        return random.sample(data, int(fraction * len(data)))


def get_date_from_buildid(bid):
    """Get a date from buildid

    Args:
        bid (str): build_id

    Returns:
        date: date object
    """
    # 20160407164938 == 2016 04 07 16 49 38
    bid = str(bid)
    year = int(bid[:4])
    month = int(bid[4:6])
    day = int(bid[6:8])
    hour = int(bid[8:10])
    minute = int(bid[10:12])
    second = int(bid[12:14])

    return __pacific.localize(datetime(year, month, day, hour, minute, second)).astimezone(pytz.utc)


def get_buildid_from_date(d):
    """Get a buildid from a date

    Args:
        d (datetime.datetime): the date

    Returns:
        str: the build_id
    """
    return d.astimezone(__pacific).strftime('%Y%m%d%H%M%S')


def rate(x, y):
    """ Compute a rate

    Args:
        x (num): numerator
        y (num): denominator

    Returns:
        float: x / y or Nan if y == 0
    """
    return float(x) / float(y) if y else float('nan')


def get_guttenberg_death():
    return get_date_ymd('1468-02-03T00:00:00Z')


def signatures_parser(signatures):
    _set = set()
    if signatures:
        signatures = map(lambda s: s.strip(' \t\r\n'), signatures.split('[@'))
        signatures = map(lambda s: s[:-1].strip(' \t\r\n'), filter(None, signatures))
        for s in filter(None, signatures):
            _set.add(s)
    return list(_set)
