import os
import pickle
from time import time
from typing import Any
from functools import lru_cache

import jesse.helpers as jh


class Cache:
    def __init__(self, path: str) -> None:
        self.path = path
        self.driver = jh.get_config('env.caching.driver', 'pickle')

        if self.driver == 'pickle':
            # make sure path exists
            os.makedirs(path, exist_ok=True)

            # if cache_database exists, load the dictionary
            if os.path.isfile(f"{self.path}cache_database.pickle"):
                with open(f"{self.path}cache_database.pickle", 'rb') as f:
                    self.db = pickle.load(f)
            # if not, create a dict object. We'll create the file when using set_value()
            else:
                self.db = {}

    def set_value(self, key: str, data: Any, expire_seconds: int = 60 * 60) -> None:
        if self.driver is None:
            return

        # add record into the database
        expire_at = None if expire_seconds is None else time() + expire_seconds
        data_path = f"{self.path}{key}.pickle"
        self.db[key] = {
            'expire_seconds': expire_seconds,
            'expire_at': expire_at,
            'path': data_path,
        }
        self._update_db()

        # store file
        with open(data_path, 'wb') as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)

    def get_value(self, key: str) -> Any:
        if self.driver is None:
            return

        try:
            item = self.db[key]
        except KeyError:
            return False

        # if expired, remove file, and database record
        # if item['expire_at'] is not None and time() > item['expire_at']:
        #     os.remove(item['path'])
        #     del self.db[key]
        #     self._update_db()
        #     return False

        # renew cache expiration time
        # if item['expire_at'] is not None:
        #     item['expire_at'] = time() + item['expire_seconds']
        #     self._update_db()

        with open(item['path'], 'rb') as f:
            return pickle.load(f)

    def _update_db(self) -> None:
        # store/update database
        with open(f"{self.path}cache_database.pickle", 'wb') as f:
            pickle.dump(self.db, f, protocol=pickle.HIGHEST_PROTOCOL)

    def flush(self) -> None:
        if self.driver is None:
            return

        for key, item in self.db.items():
            os.remove(item['path'])
        self.db = {}

    def slice_pickles(self, cache_key: str, start_date_str: str, finish_date_str: str, key: str,
                      warm_up: bool = False) -> Any:
        if self.driver is None:
            return

        try:
            item = self.db[cache_key]
            # print('item:', item)
            # if expired, remove file, and database record
            # if item['expire_at'] is not None and time() > item['expire_at']:
            #     os.remove(item['path'])
            #     del self.db[cache_key]
            #     self._update_db()
            #     return False

            # renew cache expiration time
            # if item['expire_at'] is not None:
            #     item['expire_at'] = time() + item['expire_seconds']
            #     self._update_db()

            with open(item['path'], 'rb') as f:
                return pickle.load(f)
        except KeyError:
            candidates = [pickl for pickl in self.db if key in pickl]
            # print('Candidates: ', candidates)

            start_date = jh.date_to_timestamp(start_date_str)
            finish_date = jh.date_to_timestamp(finish_date_str) - 60000

            if warm_up:
                finish_date += 60_000 * 1440

            candidate_finishdate = 0
            candidate_startdate = 0
            parent = {}
            got_parent = False

            for p in candidates:
                candidate_dates = p.split(key)[0].split('-')
                # print(candidate_dates)
                candidate_startdate_str = f'{candidate_dates[0]}-{candidate_dates[1]}-{candidate_dates[2]}'
                candidate_finishdate_str = f'{candidate_dates[3]}-{candidate_dates[4]}-{candidate_dates[5]}'
                # print('candidate start-finish dates in str: ', candidate_startdate_str, candidate_finishdate_str)
                candidate_startdate = jh.date_to_timestamp(candidate_startdate_str)
                candidate_finishdate = jh.date_to_timestamp(candidate_finishdate_str)
                if start_date >= candidate_startdate and finish_date <= candidate_finishdate:
                    parent = p
                    print('Found a parent!', parent)
                    break

            if parent:
                try:
                    item = self.db[parent]
                    # print('item:', item)

                    # if expired, remove file, and database record
                    # if item['expire_at'] is not None and time() > item['expire_at']:
                    #     os.remove(item['path'])
                    #     del self.db[cache_key]
                    #     self._update_db()
                    #     return False

                    # renew cache expiration time
                    # if item['expire_at'] is not None:
                    #     item['expire_at'] = time() + item['expire_seconds']
                    #     self._update_db()

                    with open(item['path'], 'rb') as f:
                        parent_pickles = pickle.load(f)

                    slice_len = int((finish_date / 60_000) - (start_date / 60_000))
                    slice_start = int((start_date / 60_000) - (candidate_startdate / 60_000))
                    slice_end = int(candidate_finishdate / 60_000 - finish_date / 60_000)  # for -slice from end
                    slice_finish = slice_start + slice_len  # wut??????????????????
                    pickle_slice = parent_pickles[slice_start:slice_finish + 1]

                    print(f'Slice Start: {slice_start} Finish: {slice_finish} | Calculated Slice len: {slice_len} | '
                          f'Slice len: {len(pickle_slice) / 60 / 24} days | Orphan startdate: {start_date} '
                          f'Parent startdate: {candidate_startdate}')
                    print('Len parent_pickles', len(parent_pickles))

                    # print('with enum', parent_pickles[index])
                    """print('Start with calc', parent_pickles[slice_start],
                          datetime.datetime.fromtimestamp(parent_pickles[slice_start][0] / 1000))
                    print('Finish with calc', parent_pickles[slice_finish],
                          datetime.datetime.fromtimestamp(parent_pickles[slice_finish][0] / 1000))"""
                    return pickle_slice
                except KeyError:
                    return False
            return False


cache = Cache("storage/temp/")


# Using functools.lru_cache
def cached(method):
    def decorated(self, *args, **kwargs):
        cached_method = self._cached_methods.get(method)
        if cached_method is None:
            cached_method = lru_cache()(method)
            self._cached_methods[method] = cached_method
        return cached_method(self, *args, **kwargs)

    return decorated
