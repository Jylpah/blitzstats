#!/usr/bin/env python3.8

# Script Prune stats from the DB per release 

import sys, os, argparse, datetime, json, inspect, pprint, aiohttp, asyncio, aiofiles
import aioconsole, re, logging, time, xmltodict, collections, pymongo, motor.motor_asyncio
import ssl, configparser
from datetime import date
import blitzutils as bu
from blitzutils import BlitzStars

N_WORKERS = 4
MAX_RETRIES = 3
logging.getLogger("asyncio").setLevel(logging.DEBUG)

FILE_CONFIG = 'blitzstats.ini'

DB_C_ACCOUNTS   		= 'WG_Accounts'
DB_C_UPDATES            = 'WG_Releases'
DB_C_PLAYER_STATS		= 'WG_PlayerStats'
DB_C_PLAYER_ACHIVEMENTS	= 'WG_PlayerAchievements'
DB_C_TANK_STATS     	= 'WG_TankStats'
DB_C_STATS_2_DEL        = 'Stats2Delete'
DB_C_BS_PLAYER_STATS   	= 'BS_PlayerStats'
DB_C_BS_TANK_STATS     	= 'BS_PlayerTankStats'
DB_C_TANKS     			= 'Tankopedia'
DB_C_TANK_STR			= 'WG_TankStrs'
DB_C_ERROR_LOG			= 'ErrorLog'
DB_C_UPDATE_LOG			= 'UpdateLog'


MODE_TANK_STATS         = 'tank_stats'
MODE_PLAYER_STATS       = 'player_stats'
MODE_PLAYER_ACHIEVEMENTS= 'player_achievements'

DB_C = {    MODE_TANK_STATS             : DB_C_TANK_STATS, 
            MODE_PLAYER_STATS           : DB_C_PLAYER_STATS,
            MODE_PLAYER_ACHIEVEMENTS    : DB_C_PLAYER_ACHIVEMENTS 
        }

CACHE_VALID = 24*3600*7   # 7 days

bs = None

TODAY = datetime.datetime.utcnow().date()
DEFAULT_DAYS_DELTA = datetime.timedelta(days=90)
DATE_DELTA = datetime.timedelta(days=7)
STATS_START_DATE = datetime.datetime(2014,1,1)

STATS_PRUNED = dict()
DUPS_FOUND = dict()
for stat_type in DB_C.keys():
    STATS_PRUNED[stat_type]  = 0
    DUPS_FOUND[stat_type]    = 0


# main() -------------------------------------------------------------


async def main(argv):
    # set the directory for the script
    os.chdir(os.path.dirname(sys.argv[0]))

    parser = argparse.ArgumentParser(description='Prune stats from the DB by update')
    parser.add_argument('--mode', default=['tank_stats'], nargs='+', choices=DB_C.keys(), help='Select type of stats to export')
    parser.add_argument( '-n', '--no_analyze', 	action='store_true', default=False, help='Skip analyzing the database (default FALSE: i.e. to analyze)')
    parser.add_argument( '-p', '--prune', 	action='store_true', default=False, help='Actually Prune database i.e. DELETE DATA (default is FALSE)')
    parser.add_argument('updates', metavar='X.Y [Z.D ...]', type=str, nargs='*', help='List of updates to prune')
    arggroup = parser.add_mutually_exclusive_group()
    arggroup.add_argument( '-d', '--debug', 	action='store_true', default=False, help='Debug mode')
    arggroup.add_argument( '-v', '--verbose', 	action='store_true', default=False, help='Verbose mode')
    arggroup.add_argument( '-s', '--silent', 	action='store_true', default=False, help='Silent mode')
    
    args = parser.parse_args()
    bu.set_log_level(args.silent, args.verbose, args.debug)
    bu.set_progress_step(100)

    
    try:
		## Read config
        config = configparser.ConfigParser()
        config.read(FILE_CONFIG)
        configDB    = config['DATABASE']
        DB_SERVER   = configDB.get('db_server', 'localhost')
        DB_PORT     = configDB.getint('db_port', 27017)
        DB_SSL      = configDB.getboolean('db_ssl', False)
        DB_CERT_REQ = configDB.getint('db_ssl_req', ssl.CERT_NONE)
        DB_AUTH     = configDB.get('db_auth', 'admin')
        DB_NAME     = configDB.get('db_name', 'BlitzStats')
        DB_USER     = configDB.get('db_user', None)
        DB_PASSWD   = configDB.get('db_password', None)
        DB_CERT		= configDB.get('db_ssl_cert_file', None)
        DB_CA		= configDB.get('db_ssl_ca_file', None)
    except Exception as err:
        bu.error('Error reading config file', err)

    try:
        #### Connect to MongoDB
        if (DB_USER==None) or (DB_PASSWD==None):
            client = motor.motor_asyncio.AsyncIOMotorClient(DB_SERVER,DB_PORT, ssl=DB_SSL, ssl_cert_reqs=DB_CERT_REQ, ssl_certfile=DB_CERT, tlsCAFile=DB_CA)
        else:
            client = motor.motor_asyncio.AsyncIOMotorClient(DB_SERVER,DB_PORT, authSource=DB_AUTH, username=DB_USER, password=DB_PASSWD, ssl=DB_SSL, ssl_cert_reqs=DB_CERT_REQ, ssl_certfile=DB_CERT, tlsCAFile=DB_CA)
        
        db = client[DB_NAME]
        bu.debug(str(type(db)))

        await db[DB_C_STATS_2_DEL].create_index('id')	

        if not args.no_analyze:
            tasks = []
            updates = await mk_update_list(db, args.updates)
            tankQ = None
            for u in updates:           
                bu.verbose_std('Processing update ' + u['update'])
                if bu.is_normal():
                    bu.set_counter('Stats processed: ')   
                
                if  MODE_TANK_STATS in args.mode:
                    tankQ    = await mk_tankQ(db)

                workers = 0
                # Does not work in parallel        
                if MODE_PLAYER_ACHIEVEMENTS in args.mode:
                    tasks.append(asyncio.create_task(analyze_player_achievements_WG(workers, db, u, args.prune)))
                    bu.debug('Task ' + str(workers) + ' started: analyze_player_achievements_WG()')
                    workers += 1
                if MODE_PLAYER_STATS in args.mode:
                    # NOT IMPLEMENTED YET
                    tasks.append(asyncio.create_task(analyze_player_stats_WG(workers, db, u)))
                    bu.debug('Task ' + str(workers) + ' started: analyze_player_stats_WG()')
                    workers += 1
                while workers < N_WORKERS:
                    if MODE_TANK_STATS in args.mode:
                        tasks.append(asyncio.create_task(analyze_tank_stats_WG(workers, db, u, tankQ, args.prune)))
                        bu.debug('Task ' + str(workers) + ' started: analyze_tank_stats_WG()')
                    workers += 1    # Can do this since only MODE_TANK_STATS is running in parallel                   
                
                bu.debug('Waiting for workers to finish')
                await asyncio.wait(tasks)       
                bu.debug('Cancelling workers')
                for task in tasks:
                    task.cancel()
                bu.debug('Waiting for workers to cancel')
                if len(tasks) > 0:
                    await asyncio.gather(*tasks, return_exceptions=True)
                
                bu.finish_progress_bar()
                print_stats_analyze(args.mode)
    
        # do the actual pruning and DELETE DATA
        if args.prune:
            bu.verbose_std('Starting to prune in 3 seconds. Press CTRL + C to CANCEL')
            for i in range(3):
                print(str(i) + '  ', end='', flush=True)
                time.sleep(1)
            print('')
            await prune_stats(db, args)
            print_stats_prune(args.mode)
    except KeyboardInterrupt:
        bu.finish_progress_bar()
        bu.verbose_std('\nExiting..')
    except asyncio.CancelledError as err:
        bu.error('Queue gets cancelled while still working.')
    except Exception as err:
        bu.error('Unexpected Exception', exception=err)

    return None


async def mk_update_list(db : motor.motor_asyncio.AsyncIOMotorDatabase, updates2process : list) -> list:
    """Create update queue for database queries"""

    if (len(updates2process) == 0):
        bu.error('No updates given to prune')
        return list()
    bu.debug(str(updates2process))
    # if len(updates2process) == 1:
    #     p_updates_since = re.compile('\\d+\\.\\d+\\+$')
    #     if p_updates_since.match(updates2process[0]) != None:
    #         updates2process[0] = updates2process[0][:-1]
    #         updates_since = True
    updates2process = set(updates2process)
    updates = list()
    try:
        bu.debug('Fetching updates from DB')        
        dbc = db[DB_C_UPDATES]
        cursor = dbc.find( {} , { '_id' : 0 }).sort('Date', pymongo.ASCENDING )
        cut_off_prev = 0
        bu.debug('Iterating over updates')
        first_update_found = False
        last_update_found  = False
        async for doc in cursor:
            cut_off = doc['Cut-off']
            update = doc['Release']
            if update + '+' in updates2process:
                updates2process.remove(update + '+')
                first_update_found = True
            if update + '-' in updates2process:
                if not first_update_found:
                    bu.error('"update_A-" can only be used in conjuction with "update_B+"')
                updates2process.remove(update + '-')
                last_update_found = True
            ## first_update_found has to be set for the update- to work
            if first_update_found or (update in updates2process):
                if (cut_off == None) or (cut_off == 0):
                    cut_off = bu.NOW()
                updates.append({'update': update, 'start': cut_off_prev, 'end': cut_off})
                try: 
                    if not first_update_found:
                        updates2process.remove(update)
                except KeyError as err:
                    bu.error(exception=err)
                if last_update_found:
                    first_update_found = False
                    last_update_found = False
            cut_off_prev = cut_off

    except Exception as err:
        bu.error(exception=err)
    if len(updates2process) > 0:
        bu.error('Unknown update values give: ' + ', '.join(updates2process))
    return updates


async def mk_tankQ(db : motor.motor_asyncio.AsyncIOMotorDatabase) -> asyncio.Queue:
    """Create TANK queue for database queries"""

    tankQ = asyncio.Queue()
    try:
        for tank_id in await get_tanks_DB(db):
            await tankQ.put(tank_id)            
    except Exception as err:
        bu.error(exception=err)
    bu.debug('Tank queue created: ' + str(tankQ.qsize()))
    return tankQ


async def mk_accountQ(db : motor.motor_asyncio.AsyncIOMotorDatabase, step: int = 1e7) -> asyncio.Queue:
    """Create ACCOUNT_ID queue for database queries"""    
    accountQ = asyncio.Queue()
    try:
        for min in range(0,4e9-step, step):
            await accountQ.put({'min': min, 'max': min + step})            
    except Exception as err:
        bu.error(exception=err)
    bu.debug('Account_id queue created')    
    return accountQ


def print_stats_analyze(stat_types : list = list()):
    for stat_type in stat_types:
        bu.verbose_std(stat_type + ': ' + str(DUPS_FOUND[stat_type]) + ' new duplicates found')
        DUPS_FOUND[stat_type] = 0
    

def print_stats_prune(stat_types : list = list()):
    for stat_type in stat_types:
        bu.verbose_std(stat_type + ': ' + str(STATS_PRUNED[stat_type]) + ' duplicates removed')
        STATS_PRUNED[stat_type] = 0


async def analyze_tank_stats_WG(workerID: int, db: motor.motor_asyncio.AsyncIOMotorDatabase, update: dict, tankQ: asyncio.Queue, prune : bool = False):
    """Async Worker to fetch player tank stats"""
    dbc = db[DB_C_TANK_STATS]
    stat_type = MODE_TANK_STATS

    try:
        start   = update['start']
        end     = update['end']
        update  = update['update']
    except Exception as err:
        bu.error('Unexpected Exception: ' + str(type(err)) + ' : ' + str(err), id=workerID)
        return None    

    while not tankQ.empty():
        try:
            tank_id = await tankQ.get()
            bu.debug(str(tankQ.qsize())  + ' tanks to process', id=workerID)
                
            pipeline = [    {'$match': { '$and': [  {'tank_id': tank_id }, {'last_battle_time': {'$lte': end}}, {'last_battle_time': {'$gt': start}} ] }},
                            { '$project' : { 'account_id' : 1, 'tank_id' : 1, 'last_battle_time' : 1}},
                            { '$sort': {'account_id': 1, 'last_battle_time': -1} }
                        ]
            cursor = dbc.aggregate(pipeline, allowDiskUse=True)

            account_id_prev = -1
            entry_prev = mk_log_entry(stat_type, { 'account_id': -1})        
            dups_counter = 0
            async for doc in cursor:
                if bu.is_normal():
                    bu.print_progress()
                account_id = doc['account_id']
                if bu.is_debug():
                    entry = mk_log_entry(stat_type, { 'account_id' : account_id, 'last_battle_time' : doc['last_battle_time'], 'tank_id' : doc['tank_id']})
                if account_id == account_id_prev:
                    # Older doc found!
                    if bu.is_debug():
                        bu.debug('Duplicate found: --------------------------------', id=workerID)
                        bu.debug(entry + ' : Old (to be deleted)', id=workerID)
                        bu.debug(entry_prev + ' : Newer', id=workerID)
                    await add_stat2del(workerID, db, stat_type, doc['_id'], prune)
                    dups_counter += 1
                account_id_prev = account_id 
                if bu.is_debug():
                    entry_prev = entry

        except Exception as err:
            bu.error('Unexpected Exception: ' + str(type(err)) + ' : ' + str(err), id=workerID)
        finally:
            bu.debug('Tank_id=' + str(tank_id) + ' processed: ' + str(dups_counter) + ' duplicates found', id = workerID)
            tankQ.task_done()

    return None


async def analyze_player_achievements_WG(workerID: int, db: motor.motor_asyncio.AsyncIOMotorDatabase, update: dict, prune : bool = False):
    """Async Worker to fetch player achievement stats"""
    dbc = db[DB_C_PLAYER_ACHIVEMENTS]
    stat_type = MODE_PLAYER_ACHIEVEMENTS

    try:
        start   = update['start']
        end     = update['end']
        update  = update['update']
        
        pipeline = [    {'$match': { '$and': [  {'updated': {'$lte': end}}, {'updated': {'$gt': start}} ] }},
                        { '$project' : { 'account_id' : 1, 'updated' : 1}},
                        { '$sort': {'account_id': 1, 'updated': -1} } 
                    ]
        cursor = dbc.aggregate(pipeline, allowDiskUse=True)

        account_id_prev = -1 
        entry_prev = mk_log_entry(stat_type, { 'account_id': -1}) 
        dups_counter = 0       
        async for doc in cursor:    
            if bu.is_normal():
                bu.print_progress()
            account_id = doc['account_id']
            if bu.is_debug():
                entry = mk_log_entry(stat_type, { 'account_id' : account_id, 'updated' : doc['updated']})
            if account_id == account_id_prev:
                # Older doc found!
                if bu.is_debug():
                    bu.debug('Duplicate found: --------------------------------', id=workerID)
                    bu.debug(entry + ' : Old (to be deleted)', id=workerID)
                    bu.debug(entry_prev + ' : Newer', id=workerID)
                await add_stat2del(workerID, db, stat_type, doc['_id'], prune)
                dups_counter += 1                
            account_id_prev = account_id
            if bu.is_debug():
                entry_prev = entry 

    except Exception as err:
        bu.error('Unexpected Exception', exception=err, id=workerID)
    finally:
        bu.debug( stat_type + ': ' + str(dups_counter) + ' duplicates found for update ' + update, id = workerID)          

    return None


async def analyze_player_stats_WG(workerID: int, db: motor.motor_asyncio.AsyncIOMotorDatabase, update: dict):
    bu.error('NOT IMPLEMENTED YET')
    pass


async def add_stat2del(workerID: int, db: motor.motor_asyncio.AsyncIOMotorDatabase, stat_type: str, id: str, prune : bool = False):
    """Adds _id of the stat record to be deleted in into DB_C_STATS_2_DEL"""
    global DUPS_FOUND, STATS_PRUNED

    dbc = db[DB_C_STATS_2_DEL]
    dbc2prune = db[DB_C[stat_type]]
    try:
        if prune:
            res = await dbc2prune.delete_one( { '_id': id } )
            STATS_PRUNED[stat_type] += res.deleted_count
        else:
            await dbc.insert_one({'type': stat_type, 'id': id})
            DUPS_FOUND[stat_type] += 1
    except Exception as err:
        bu.error(exception=err, id=workerID)
    return None


async def prune_stats(db: motor.motor_asyncio.AsyncIOMotorDatabase, args : argparse.Namespace):
    """Execute DB pruning and DELETING DATA. Does NOT verify whether there are newer stats"""
    global STATS_PRUNED
    try:
        batch_size = 200
        dbc = db[DB_C_STATS_2_DEL]
        for stat_type in args.mode:
            bu.set_counter(stat_type + ' pruned: ')            
            dbc2prune = db[DB_C[stat_type]]
            cursor = dbc.find({'type' : stat_type}).batch_size(batch_size)
            docs = await cursor.to_list(batch_size)
            while docs:
                ids = set()
                for doc in docs:
                    ids.add(doc['id'])
                    bu.print_progress()
                if len(ids) > 0:
                    try:
                        res = await dbc2prune.delete_many( { '_id': { '$in': list(ids) } } )
                        STATS_PRUNED[stat_type] += res.deleted_count
                    except Exception as err:
                        bu.error('Failure in deleting ' + stat_type, exception=err)
                    try:
                        await dbc.delete_many({ 'type': stat_type, 'id': { '$in': list(ids) } })
                    except Exception as err:
                        bu.error('Failure in clearing stats-to-be-pruned table')
                docs = await cursor.to_list(batch_size)
            bu.finish_progress_bar()

    except Exception as err:
        bu.error(exception=err)
    return None


# def mk_log_entry(stat_type: str = None, account_id=None, last_battle_time=None, tank_id = None):
def mk_log_entry(stat_type: str = None, stats: dict = None):
    try:
        entry = stat_type + ': '
        for key in stats:
            entry = entry + ' : ' + key + '=' + str(stats[key])
        return entry
    except Exception as err:
        bu.error(exception=err)
        return None

async def get_tanks_DB(db: motor.motor_asyncio.AsyncIOMotorDatabase):
    """Get tank_ids of tanks in the DB"""
    dbc = db[DB_C_TANK_STATS]
    return await dbc.distinct('tank_id')
    

# main()
if __name__ == "__main__":
    #asyncio.run(main(sys.argv[1:]), debug=True)
    asyncio.run(main(sys.argv[1:]))