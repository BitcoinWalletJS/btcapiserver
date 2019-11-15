import asyncio
import signal
from setproctitle import setproctitle
import asyncpg
from collections import deque
from pybtc import bytes_to_int, sha256, map_into_range, siphash, double_sha256
from pybtc import ripemd160, int_to_var_int, encode_gcs
import time
import traceback
from sortedcontainers import *



class FilterCompressor():

    def __init__(self, dsn, logger):
        setproctitle('btcapi server: filter compressor')
        policy = asyncio.get_event_loop_policy()
        policy.set_event_loop(policy.new_event_loop())
        self.dsn = dsn
        self.log = logger

        self.batch_size =  144 * 7
        self.map_range = 2 ** 32

        self.active = True
        self.db_pool = None
        self.start_time = time.time()
        self.last_pointer = 0

        self.compressor_task = None
        self.loop = asyncio.get_event_loop()
        signal.signal(signal.SIGTERM, self.terminate)
        self.loop.create_task(self.start())
        self.loop.run_forever()


    async def start(self):
        try:
            self.db_pool = await asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=2)
            self.log.info("Filter compressor module started")
            self.batch_task = self.loop.create_task(self.batch_compressor())
        except Exception as err:
            self.log.warning("Start filter compressor module failed: %s" % err)
            await asyncio.sleep(3)
            self.loop.create_task(self.start())


    async def batch_compressor(self):
        self.log.info("batch compressor started")

        try:
            n_type_map_filter_type = {0: 2, 1: 4, 2: 1, 5: 8, 6: 16}

            tts = 0
            batch_size = self.batch_size
            F = self.map_range

            last_height = - 1
            total_elements_count, total_elements_size = 0, 0
            total_duplicates_count, total_duplicates_size = 0, 0
            last_hash = {1: None, 2: None, 4: None, 8: None,  16: None}

            # load last filters hash
            async with self.db_pool.acquire() as conn:
                h = await conn.fetchval("SELECT height FROM block_filter  ORDER BY height DESC LIMIT 1;")
                if h is not None:
                    rows = await conn.fetch("SELECT type, hash FROM block_filter where height = $1;", h)
                    for row in rows:
                        last_hash[row["type"]] = row["hash"]


            while True:
                batch_map = {1: dict(), 2: dict(), 4: dict(), 8: dict(), 16:dict()}
                records = deque()
                element_index = {1: 0, 2: 0, 4: 0, 8: 0, 16: 0}

                try:
                    async with self.db_pool.acquire() as conn:
                        h = await conn.fetchval("SELECT height FROM block_filter  ORDER BY height DESC LIMIT 1;")
                        if h is not None:
                            last_batch_height = (h // batch_size) * batch_size
                            last_height = last_batch_height + batch_size - 1
                            if h != last_height:
                                await asyncio.sleep(60)
                                continue

                    async with self.db_pool.acquire() as conn:
                        blocks = await conn.fetch("SELECT blocks.height, filter "
                                                  "FROM raw_block_filters "
                                                  "JOIN blocks ON blocks.height = raw_block_filters.height "
                                                  "WHERE raw_block_filters.height > $1 "
                                                  "ORDER BY raw_block_filters.height ASC LIMIT $2;",
                                                  last_height, batch_size)

                        if len(blocks) != batch_size:
                            await asyncio.sleep(60)
                            continue

                        elements_count, elements_size = 0, 0
                        duplicates_count, duplicates_size = 0, 0


                        for block in blocks:
                            raw_elements = {1: SortedSet(), 2: SortedSet(),
                                            4: SortedSet(), 8: SortedSet(), 16: SortedSet()}
                            duplicates = {1: set(), 2: set(), 4: set(), 8: set(), 16:set()}
                            tx_filters = {1: dict(), 2: dict(), 4: dict(), 8: dict(), 16:dict()}
                            assert last_height + 1 == block["height"]

                            last_height = block["height"]

                            for re in set([block["filter"][i:i + 25] for i in range(0, len(block["filter"]), 25)]):
                                f_type = n_type_map_filter_type[re[0]]
                                e = map_into_range(siphash(re[5:]),  F)

                                if e in batch_map[f_type]:
                                    duplicates[f_type].add(e)
                                else:
                                    raw_elements[f_type].add(e)
                                    elements_count += 1
                                tx_index = bytes_to_int(re[1:5], "little")
                                try:
                                    tx_filters[f_type][tx_index].add(e.to_bytes(4, "little"))
                                except:
                                    tx_filters[f_type][tx_index] = SortedSet({e.to_bytes(4, "little")})


                            for f_type in raw_elements:
                                if raw_elements[f_type]:
                                    for x in raw_elements[f_type]:
                                        batch_map[f_type][x] = element_index[f_type]
                                        element_index[f_type] += 1
                                    d = encode_gcs(raw_elements[f_type], sort=False)
                                    f = b"".join([int_to_var_int(len(d)), d])
                                    elements_size += len(f)
                                else:
                                    f = int_to_var_int(0)

                                if duplicates[f_type]:
                                    pointers_set = set()
                                    # convert values to pointers
                                    for x in duplicates[f_type]:
                                        pointers_set.add(batch_map[f_type][x])

                                    encoded_pointers= encode_gcs(pointers_set)
                                    fd =  b"".join([int_to_var_int(len(encoded_pointers)),
                                                    encoded_pointers])
                                    f += fd
                                    duplicates_size += len(fd)
                                    duplicates_count += len(duplicates[f_type])
                                else:
                                    f += int_to_var_int(0)

                                if f != b"\x00\x00":
                                    d = bytearray()
                                    for i in sorted(tx_filters[f_type].keys()):
                                        d += b"".join(tx_filters[f_type][i])
                                    f += ripemd160(sha256(d))

                                if last_hash[f_type]:
                                    last_hash[f_type] = double_sha256(double_sha256(f) + last_hash[f_type])
                                elif f != b"\x00\x00":
                                    last_hash[f_type] = double_sha256(double_sha256(f) + b"\00" * 32)

                                if last_hash[f_type]:
                                    records.append((last_height, f_type, last_hash[f_type], f))

                                tts += len(f)

                        async with conn.transaction():
                            await conn.copy_records_to_table('block_filter',
                                                             columns=["height", "type", "hash", "filter"],
                                                             records=records)
                            await conn.execute("DELETE FROM raw_block_filters WHERE height >= $1 and height < $2;",
                                               last_height - batch_size, last_height)

                        total_elements_count += elements_count
                        total_elements_size += elements_size
                        total_duplicates_count += duplicates_count
                        total_duplicates_size += duplicates_size

                        self.log.info("Created block filters batch %s; Range %s -> %s;" % (last_height // batch_size,
                                                                                           last_height - batch_size,
                                                                                           last_height))
                        self.log.debug("    Elements: %s;  bytes per element %s" %
                                       (elements_count, round(elements_size/elements_count, 4)))

                        if duplicates_count:
                            self.log.debug("    Duplicates: %s;  "
                                           "bytes per duplicate %s" % (duplicates_count,
                                                                       round(duplicates_size/duplicates_count, 4)))
                        self.log.debug("    Size: %s; bytes per address %s" %
                                       (duplicates_count + elements_count,
                                        round((duplicates_size+elements_size)/(duplicates_count+elements_count), 4)))
                        self.log.debug("    ----------------------------------------------------------------")
                        self.log.debug("    Cumulative:")
                        self.log.debug("        Elements: %s;  bytes per element %s" %
                                       (total_elements_count,
                                        round(total_elements_size/total_elements_count, 4)))
                        if total_duplicates_count:
                            self.log.debug("        Duplicates: %s;  bytes per duplicate %s" %
                                           (total_duplicates_count,
                                            round(total_duplicates_size/total_duplicates_count, 4)))
                        self.log.debug("        Total size: %s;  bytes per address %s" %
                                       (tts, round(tts/(total_duplicates_count+total_elements_count), 4)))
                        self.log.debug("    ----------------------------------------------------------------")


                except asyncio.CancelledError:
                    self.log.warning("Filter compressor module canceled")
                    break

                except Exception as err:
                    self.log.error("filter compressor error: %s" % err)
                    print(traceback.format_exc())
                    await asyncio.sleep(10)

        except:
            print(traceback.format_exc())


    def terminate(self, a, b):
        self.loop.create_task(self.terminate_coroutine())

    async def terminate_coroutine(self):
        self.active = False
        if self.compressor_task:
            self.compressor_task.cancel()
            r = await self.compressor_task
            try: r.result()
            except: pass
        self.log.info("Filter compressor module stopped")
        self.loop.stop()
