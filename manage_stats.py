#!/usr/bin/env python3

# Script Prune stats from the DB per release 

import sys, os, argparse, datetime, json, inspect, pprint, aiohttp, asyncio, aiofiles
import aioconsole, re, logging, time, xmltodict, collections, pymongo, motor.motor_asyncio
import ssl, configparser, random
from datetime import date
import blitzutils as bu
import blitzstatsutils as su
from blitzutils import BlitzStars, RecordLogger, WG, Timer

N_WORKERS = 4

logging.getLogger("asyncio").setLevel(logging.DEBUG)

FILE_CONFIG = 'blitzstats.ini'

CACHE_VALID     = 7*24*3600   # 7 days
DEFAULT_SAMPLE  = 1000
QUEUE_LEN       = 10000
DEFAULT_BATCH   = 1000
MODES = [ su.MODE_TANK_STATS, su.MODE_PLAYER_ACHIEVEMENTS ]
bs = None

TODAY               = datetime.datetime.utcnow().date()
DEFAULT_DAYS_DELTA  = datetime.timedelta(days=90)
DATE_DELTA          = datetime.timedelta(days=7)
STATS_START_DATE    = datetime.datetime(2014,1,1)

def def_value_zero():
    return 0


#####################################################################
#                                                                   #
# main()                                                            #
#                                                                   #
#####################################################################

async def main(argv):
    # set the directory for the script
    timer = Timer('Execution time')
    current_dir = os.getcwd()
    os.chdir(os.path.dirname(sys.argv[0]))

    parser = argparse.ArgumentParser(description='Manage DB stats')
    parser.add_argument('--mode', default=[su.MODE_TANK_STATS], nargs='+', choices=MODES, help='Select type of stats to process')
    
    arggroup_action = parser.add_mutually_exclusive_group(required=True)
    arggroup_action.add_argument( '--analyze',  action='store_true', default=False, help='Analyze the database for duplicates')
    arggroup_action.add_argument( '--check', 	action='store_true', default=False, help='Check the analyzed duplicates')
    arggroup_action.add_argument( '--prune', 	action='store_true', default=False, help='Prune database for the analyzed duplicates i.e. DELETE DATA')
    arggroup_action.add_argument( '--snapshot',	action='store_true', default=False, help='Snapshot latest stats from the archive')
    arggroup_action.add_argument( '--archive',	action='store_true', default=False, help='Archive latest stats')
    arggroup_action.add_argument( '--clean',	action='store_true', default=False, help='Clean latest stats from old stats')
    arggroup_action.add_argument( '--reset', 	action='store_true', default=False, help='Reset the analyzed duplicates')
    arggroup_action.add_argument( '--initdb', 	action='store_true', default=False, help='Initialize database indexes')

    parser.add_argument('--opt_tanks',   default=None, nargs='*', type=str, help='List of tank_ids for other options. Use "tank_id+" to start from a tank_id')
    parser.add_argument('--opt_archive', action='store_true', default=False, help='Process stats archive')

    arggroup_verbosity = parser.add_mutually_exclusive_group()
    arggroup_verbosity.add_argument( '-d', '--debug', 	action='store_true', default=False, help='Debug mode')
    arggroup_verbosity.add_argument( '-v', '--verbose', action='store_true', default=False, help='Verbose mode')
    arggroup_verbosity.add_argument( '-s', '--silent', 	action='store_true', default=False, help='Silent mode')

    parser.add_argument('--sample', type=int, default=None, help='Sample size. Default=' + str(DEFAULT_SAMPLE) + ' . 0: check ALL.')
    parser.add_argument('-l', '--log', action='store_true', default=False, help='Enable file logging')
    parser.add_argument('updates', metavar='X.Y [Z.D ...]', type=str, nargs='*', help='List of updates to prune')
    args = parser.parse_args()

    try:
        bu.set_log_level(args.silent, args.verbose, args.debug)
        bu.set_progress_step(100)
        if args.snapshot or args.archive:
            args.log = True
        if args.log:
            datestr = datetime.datetime.now().strftime("%Y%m%d_%H%M")
            await bu.set_file_logging(bu.rebase_file_args(current_dir, 'manage_stats_' + datestr + '.log'))

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

        if  args.analyze or args.check or args.prune:
            updates = await mk_update_list(db, args.updates)
        else:
            updates = list()

        if args.analyze:
            bu.verbose_std('Starting to ANALYZE duplicates in 3 seconds. Press CTRL + C to CANCEL')
            bu.wait(3)
            await analyze_stats(db, updates, args)

        
        elif args.check:
            bu.verbose_std('Starting to CHECK duplicates in 3 seconds. Press CTRL + C to CANCEL')
            bu.wait(3)         
            await check_stats(db, updates, args)
                    
        elif args.prune:
            # do the actual pruning and DELETE DATA
            bu.verbose_std('Starting to PRUNE in 3 seconds. Press CTRL + C to CANCEL')
            bu.wait(3)
            await prune_stats(db, updates, args)
        
        elif args.snapshot:
            bu.verbose_std('Starting to SNAPSHOT ' + ', '.join(args.mode) + ' in 3 seconds. Press CTRL + C to CANCEL')
            bu.wait(3)
            if input('Write "SNAPSHOT" if you prefer to snapshot stats: ') == 'SNAPSHOT':
                if su.MODE_PLAYER_ACHIEVEMENTS in args.mode:
                    bu.verbose_std('Starting to SNAPSHOT PLAYER ACHIEVEMENTS')
                    await snapshot_player_achivements(db, args)
                if su.MODE_TANK_STATS in args.mode:
                    bu.verbose_std('Starting to SNAPSHOT TANK STATS')
                    await snapshot_tank_stats(db, args)
                await update_log(db, 'snapshot', None, args)
            else:
                bu.error('Invalid input given. Exiting...')

        elif args.archive:
            bu.verbose_std('Starting to ARCHIVE stats in 3 seconds')
            bu.wait(1)
            bu.verbose_std('Run ANALYZE + PRUNE before archive')
            bu.verbose_std('Press CTRL + C to CANCEL')
            bu.wait(2)
            if su.MODE_PLAYER_ACHIEVEMENTS in args.mode:
                await archive_player_achivements(db, args)
            if su.MODE_TANK_STATS in args.mode:
                await archive_tank_stats(db)
            await update_log(db, 'archive', None, args)
        
        elif args.reset:
            bu.verbose_std('Starting to RESET duplicates in 3 seconds. Press CTRL + C to CANCEL')
            bu.wait(3)         
            await reset_duplicates(db, updates, args)
        
        elif args.initdb:
            bu.verbose_std('Initializing DB indexes')
            await su.init_db_indices(db)
             
    except KeyboardInterrupt:
        bu.finish_progress_bar()
        bu.verbose_std('\nExiting..')
    except asyncio.CancelledError as err:
        bu.error('Queue gets cancelled while still working.')
    except Exception as err:
        bu.error('Unexpected Exception', exception=err)
    timer.stop()
    bu.verbose_std(timer.time_str(name=True))
    return None


async def update_log(db: motor.motor_asyncio.AsyncIOMotorDatabase, 
                     action: str, 
                     updates: list, 
                     args: argparse.Namespace, 
                     stat_types: list = None):
    try:
        sample = args.sample
        archive = args.opt_archive
        
        if sample != None:
            return False
        if updates == None:
            updates = [ { 'update': None } ]
        modes = args.mode
        if stat_types != None:
            modes = stat_types

        for update in updates:
            for mode in modes:
                mode_str = mode
                if archive:
                    mode_str = mode_str + su.MODE_ARCHIVE
                await su.update_log(db, action, mode_str, update['update'])
        return True
    except Exception as err:
        bu.error(exception=err) 
    return False


def mk_update_entry(update: str, start: int, end: int)  -> dict:
    """Make update entry to the update list to process"""
    if (end == None) or (end == 0):
        end = bu.NOW()
    return {'update': update, 'start': start, 'end': end }


def mk_dups_Q_entry(ids: list, update: str = None, 
                    stat_type: str = None,  
                    from_db: bool = False) -> dict:
    """Make a prune task for prune queue"""
    if (ids == None) or (len(ids) == 0):
        return None
    # bu.debug('ids: ' + ', '.join([ str(id) for id in ids ]), force=True)
    # bu.debug('ids: ' + len(ids), force=True)
    return { 'ids': ids, 'update': update, 'from_db': from_db, 'stat_type': stat_type }


def mk_dup_db_entry(stat_type: str, _id=object, update: str = None) -> dict:
    return  { 'type': stat_type, 'id': _id, 'update': update } 


async def get_latest_update(db: motor.motor_asyncio.AsyncIOMotorDatabase) -> dict:
    try:
        dbc = db[su.DB_C_UPDATES]
        cursor = dbc.find().sort('Date',-1).limit(2)
        updates = await cursor.to_list(2)
        doc = updates.pop(0)
        update = doc['Release']
        end = doc['Cut-off']
        doc = updates.pop(0)
        start = doc['Cut-off']
        return mk_update_entry(update, start, end)
    except Exception as err:
        bu.error(exception=err)   


async  def mk_update_list(db : motor.motor_asyncio.AsyncIOMotorDatabase, updates2process : list) -> list:
    """Create update queue for database queries"""
    if (len(updates2process) == 0):
        bu.verbose_std('Processing the latest update')
        return [ await get_latest_update(db) ]
    elif (len(updates2process) == 1) and (updates2process[0] == su.UPDATE_ALL):
        bu.verbose_std('Processing ALL data')
        return [ None ]
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
        dbc = db[su.DB_C_UPDATES]
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
                updates.append(mk_update_entry(update, cut_off_prev, cut_off))
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


async def mk_tankQ(db : motor.motor_asyncio.AsyncIOMotorDatabase, archive: bool = False) -> asyncio.Queue:
    """Create TANK queue for database queries"""

    tankQ = asyncio.Queue()
    try:
        for tank_id in await get_tanks_DB(db, archive):
            await tankQ.put(tank_id)            
    except Exception as err:
        bu.error(exception=err)
    bu.debug('Tank queue created: ' + str(tankQ.qsize()))
    return tankQ


async def mk_accountQ(db : motor.motor_asyncio.AsyncIOMotorDatabase, sample: int = None, active_time: int = None) -> asyncio.Queue:
    """Create ACCOUNT_ID queue for database queries"""    
    try:
        accountQ = asyncio.Queue()
        dbc         = db[su.DB_C_ACCOUNTS]

        match       = { '_id' : {  '$lt' : WG.ACCOUNT_ID_MAX}} 
        pipeline    = [ { '$match' :  match } ]
        if sample != None:
            pipeline.append({'$sample': {'size' : sample} })
        pipeline.append({ '$project': { '_id':  True }})

        cursor = dbc.aggregate(pipeline)
        async for account in cursor:
            # bu.debug('account_id: ' + str(account['_id']), force=True)
            await accountQ.put({ 'account_id': account['_id']})
    except Exception as err:
        bu.error(exception=err)
    bu.debug('Account_id queue created')    
    return accountQ


async def mk_account_rangeQ(step: int = int(5e7)) -> asyncio.Queue:
    """Create ACCOUNT_ID queue for database queries"""    
    accountQ = asyncio.Queue()
    try:
        id_max  = WG.ACCOUNT_ID_MAX
        res     = list()        
        for min in range(0,id_max-step, step):
            res.append({'min': min, 'max': min + step})
        random.shuffle(res) # randomize the results for better ETA estimate.
        for item in res:
            await accountQ.put(item)     
    except Exception as err:
        bu.error(exception=err)
    bu.debug('Account_id queue created')    
    return accountQ


async def mk_account_tankQ(db: motor.motor_asyncio.AsyncIOMotorDatabase, 
                            tank_ids: list = None, archive: bool = False, 
                            account_id_step: int = int(5e7)) -> asyncio.Queue:
    """Create a queue of ACCOUNT_ID * TANK_ID queue for database queries"""    
    retQ = asyncio.Queue()
    try:
        if tank_ids == None:
            tank_ids = await get_tanks_DB(db, archive)
        id_max = WG.ACCOUNT_ID_MAX
        res = list()       
        for min_id in range(0,id_max-account_id_step, account_id_step):
            for tank_id in tank_ids:
                res.append( { 'tank_id': tank_id, 'account_id_min': min_id, 'account_id_max': min_id + account_id_step } )
        random.shuffle(res)   # randomize the results for better ETA estimate.
        for item in res: 
            await retQ.put(item)
    except Exception as err:
        bu.error('Failed to create account_id * tank_id queue', exception=err)
        return None
    bu.debug('account_id * tank_id queue created')    
    return retQ

## REVISE ALGO: 
# 1) find only stats with (FIELD_UPDATE)
# 2) make a account_tankQ (not range). use set()
# 3) Find older stats and put to the pruce Q
# 4) Prune
# 5) Unset FIELD_UPDATE
async def mk_account_tankQ_uniq(db: motor.motor_asyncio.AsyncIOMotorDatabase, 
                            update_record: dict = None,
                            tank_ids: list = None, archive: bool = False) -> asyncio.Queue:
    """Create a queue of ACCOUNT_ID + TANK_ID queue for database queries"""    
    retQ = asyncio.Queue()
    try:
        if archive:
            bu.error('Does not work for the archive DB')
            return retQ            
        else:            
            dbc      = db[su.DB_C[su.MODE_TANK_STATS]]
        
        update_match = list()
        if update_record != None:
            update_match.append({ 'last_battle_time': { '$gt': update_record['start']}})
            update_match.append({ 'last_battle_time': { '$lte': update_record['end']}})

        if tank_ids == None:
            tank_ids = await get_tanks_DB(db)
        res = set()

        bu.set_progress_bar('Finding updated account_id/tank_id pairs', max_value=len(tank_ids), step=1, slow=True)    
        for tank_id in tank_ids:
        #for tank_id in [ 1, 17 ]:
            match = [ { su.FIELD_NEW_STATS: { '$exists': True }}, { 'tank_id': tank_id} ] + update_match
            cursor = dbc.find( { '$and': match }, 
                               { 'account_id': True, 'tank_id': True, '_id': False})
            async for stat in cursor:
                res.add( json.dumps(stat))
            bu.print_progress()
        res = list(res)
        random.shuffle(res)   # randomize the results for better ETA estimate.
        for item in res: 
            await retQ.put(json.loads(item))
    except Exception as err:
        bu.error('Failed to create account_id + tank_id queue', exception=err)
        return None
    bu.debug('account_id + tank_id queue created')    
    return retQ


async def analyze_stats(db: motor.motor_asyncio.AsyncIOMotorDatabase, 
                        updates: list, args: argparse.Namespace = None) -> RecordLogger:
    """--analyze: top-level func for analyzing stats for duplicates"""
    try:
        rl = RecordLogger('Analyze')
        if su.MODE_TANK_STATS in args.mode:
            rl.merge(await analyze_tank_stats(db, updates, args))
        
        if su.MODE_PLAYER_ACHIEVEMENTS in args.mode:
            rl.merge(await analyze_player_achievements(db, updates, args))            
        await update_log(db, 'analyze', updates, args)
    except Exception as err:
        bu.error(exception=err)
    bu.log(rl.print(do_print=False))
    rl.print()
    return None     


async def reset_duplicates(db: motor.motor_asyncio.AsyncIOMotorDatabase, 
                        updates: list, args: argparse.Namespace = None) -> RecordLogger:
    """--reset: reset/delete duplicates info from the DB"""
    try:
        rl = RecordLogger('Reset')
        archive = args.opt_archive
        dbc = db[su.DB_C_STATS_2_DEL]

        for stat_type in args.mode:
            if archive:
                stat_type = stat_type + su.MODE_ARCHIVE
            rl_mode = RecordLogger(get_mode_str(stat_type) + ' duplicates')
            res = await dbc.delete_many({'type': stat_type})
            if res != None:
                rl_mode.log('OK', res.deleted_count)
            else:
                rl_mode.log('FAILED')
            rl.merge(rl_mode)  
        await update_log(db, 'reset dups', updates, args)   
    except Exception as err:
        bu.error(exception=err)
    bu.log(rl.print(do_print=False))
    rl.print()
    return None


async def analyze_tank_stats(db: motor.motor_asyncio.AsyncIOMotorDatabase, 
                             updates: list = list(), args: argparse.Namespace = None)  -> RecordLogger:
    """--analyze: top-level func for analyzing stats for duplicates. DOES NOT PRUNE"""
    try:        
        archive     = args.opt_archive
        stat_type   = su.MODE_TANK_STATS
        rl          = RecordLogger(get_mode_str(stat_type, archive))
        dupsQ       = asyncio.Queue(QUEUE_LEN)
        dups_saver = asyncio.create_task(save_dups_worker(db, stat_type, dupsQ, archive))
        
        for u in updates: 
            try:
                if u == None:
                    update_str = su.UPDATE_ALL
                    bu.verbose_std('Analyzing ' + get_mode_str(stat_type, archive) + ' for duplicates. (ALL DATA)')
                else:
                    update_str = u['update']
                    bu.verbose_std('Analyzing ' + get_mode_str(stat_type, archive) + ' for duplicates. Update ' + update_str)
                    
                if archive:
                    #account_tankQ = await mk_account_tankQ(db)
                    account_tankQ = await mk_accountQ(db, args.sample)
                else:
                    account_tankQ = await mk_account_tankQ_uniq(db, u)                

                bu.set_progress_bar('Stats processed', account_tankQ.qsize(), step=200, slow=True)   
                
                workers = []
                for workerID in range(0, N_WORKERS):
                    workers.append(asyncio.create_task(find_dup_tank_stats_worker(db, account_tankQ, dupsQ, u, workerID, archive)))
                
                await account_tankQ.join()
                bu.finish_progress_bar()
                bu.debug('Waiting for workers to finish')
                if len(workers) > 0:
                    i = 0
                    for rl_worker in await asyncio.gather(*workers, return_exceptions=True):
                        bu.debug('Merging find_dup_tank_stats_worker\'s RecordLogger', id=i)
                        rl.merge(rl_worker)
                        i = +1                
        
            except Exception as err:
                bu.error('Update: ' + update_str, exception=err)    
        
        await dupsQ.join()
        dups_saver.cancel()
        for rl_task in await asyncio.gather(*[dups_saver], return_exceptions=True):
            rl.merge(rl_task)
    
    except Exception as err:
        bu.error(exception=err)
    return rl


async def analyze_player_achievements(db: motor.motor_asyncio.AsyncIOMotorDatabase,
                                      updates: list, args: argparse.Namespace = None) -> RecordLogger:
    try:
        archive     = args.opt_archive
        stat_type   = su.MODE_PLAYER_ACHIEVEMENTS
        rl          = RecordLogger(get_mode_str(stat_type, archive))
        dupsQ       = asyncio.Queue(QUEUE_LEN)
        dups_saver  = asyncio.create_task(save_dups_worker(db, stat_type, dupsQ, archive))
        
        for u in updates: 
            try:
                if u == None:
                    update_str = su.UPDATE_ALL
                    bu.verbose_std('Analyzing ' + get_mode_str(stat_type, archive) + ' for duplicates. (ALL DATA)')
                else:
                    update_str = u['update']
                    bu.verbose_std('Analyzing ' + get_mode_str(stat_type, archive) + ' for duplicates. Update ' + u['update'])
                accountQ = await mk_account_rangeQ()
                lenQ = accountQ.qsize()                
                bu.set_progress_bar('Stats processed', lenQ, step=5, slow=True)  
                # bu.set_counter('Stats processed: ')   
                workers = []

                for workerID in range(0, N_WORKERS):
                    workers.append(asyncio.create_task(find_dup_player_achivements_worker(db, accountQ, dupsQ, u, workerID, archive)))
                
                await accountQ.join()
                bu.debug('Waiting for workers to finish')
                if len(workers) > 0:
                    for res in await asyncio.gather(*workers, return_exceptions=True):
                        rl.merge(res) 
                bu.finish_progress_bar()
        
            except Exception as err:
                bu.error('Update: ' + update_str, exception=err)    
        
        await dupsQ.join()
        dups_saver.cancel()
        for rl_task in await asyncio.gather(*[dups_saver], return_exceptions=True):
            rl.merge(rl_task)
    
    except Exception as err:
        bu.error(exception=err)
    return rl


async def save_dups_worker( db: motor.motor_asyncio.AsyncIOMotorDatabase, 
                            stat_type: str, dupsQ: asyncio.Queue, archive: bool = False)  -> RecordLogger:
    """Save duplicates information to the DB"""
    try:
        dbc = db[su.DB_C_STATS_2_DEL]
        rl = RecordLogger('Save duplicate info')
        if archive:
            stat_type = stat_type + su.MODE_ARCHIVE
        while True:
            dups = await dupsQ.get()
            if dups['stat_type'] != stat_type:
                dupsQ.task_done()
                bu.error('Received wrong stat_type from queue: ' + dups['stat_type'])
                continue
            if 'update' in dups:
                update = dups['update']
            else:
                update = None
            try:                
                res = await dbc.insert_many( [ mk_dup_db_entry(dups['stat_type'], dup_id, update) for dup_id in dups['ids'] ] )
                rl.log('Saved', len(res.inserted_ids))
            except Exception as err:
                bu.error(exception=err)
                rl.log('Errors')
            dupsQ.task_done()
    
    except asyncio.CancelledError:
        bu.debug('Duplicate queue is empty')
    except Exception as err:
        bu.error(exception=err)
    return rl


async def get_dups_worker( db: motor.motor_asyncio.AsyncIOMotorDatabase, 
                            stat_type: str, dupsQ: asyncio.Queue, 
                            sample: int = 0, archive = False, update: str = None)  -> RecordLogger:
    """Read duplicates info from the DB"""
    try:
        #bu.debug('Started', force=True)
        dbc = db[su.DB_C_STATS_2_DEL]
        count = 0
        if archive:
            stat_type = stat_type + su.MODE_ARCHIVE
        rl = RecordLogger('Fetch ' + get_mode_str(stat_type))
        match = [{'type' : stat_type} ]
        if update != None:
            match.append({ 'update': update } )
        match.reverse() ## Index has update first
        pipeline = [  { '$match': { '$and': match } } ]
        if (sample != None) and (sample > 0):
            pipeline.append({ '$sample': { 'size': sample } })

        # timer  = Timer('Execution time', ['Active'], start=True)        
        cursor  = dbc.aggregate(pipeline)
        dups    = await cursor.to_list(DEFAULT_BATCH)
        
        #bu.debug('Starting while loop', force=True)
        while dups:
            try:
                ids  =  [ dup['id']   for dup in dups ] 
                # timer.stop('Active')               
                await dupsQ.put( mk_dups_Q_entry( ids, update=update, stat_type=stat_type, from_db=True ) )
                # timer.start('Active')
                count += len(dups)
                rl.log('Read', len(dups))
            except Exception as err:
                rl.log('Errors')
                bu.error(exception=err)
            finally:
                dups = await cursor.to_list(DEFAULT_BATCH)
                #bu.debug('while-iteration complete', force=True)
        #bu.debug('While-loop done', force=True)
        
        # timer.stop(all=True)
        # for mode in timer.get_modes():
            # rl.log(timer.get_mode_name(mode), timer.elapsed(mode))

    except (asyncio.CancelledError):
        bu.debug('Cancelled before finishing')
    except Exception as err:
        bu.error(exception=err)
    bu.debug('Exiting. Added ' + str(count) + ' duplicates')
    return rl


async def check_stats(db: motor.motor_asyncio.AsyncIOMotorDatabase, 
                      updates: list, args: argparse.Namespace = None) -> RecordLogger:
    """Parallel check for the analyzed player achivements duplicates"""
    try:
        rl      = RecordLogger('Check duplicates')        
        archive = args.opt_archive
        sample  = args.sample
        if sample == None:
            sample = DEFAULT_SAMPLE

        for stat_type in args.mode:
            for u in updates:
                try:
                    if u == None:
                        bu.verbose_std('Checking ' + get_mode_str(stat_type, archive) +  ' duplicates. (ALL DATA)')
                        update = None                         
                    else:
                        update = u['update']
                        bu.verbose_std('Checking ' + get_mode_str(stat_type, archive) +  ' duplicates for update ' + update)
        
                    bu.verbose_std('Counting duplicates ... ', eol=False)
                    N_dups = await count_dups2prune(db, stat_type, archive, update)
                    bu.verbose_std(str(N_dups) + ' found')
                    dupsQ = asyncio.Queue(QUEUE_LEN)
                    fetcher = asyncio.create_task(get_dups_worker(db, stat_type, dupsQ, sample, archive, update))

                    if (sample > 0) and (sample < N_dups):            
                        header = 'Checking sample of duplicates' 
                    else:
                        sample = N_dups
                        header = 'Checking ALL duplicates'
                    if bu.is_normal():
                        bu.set_progress_bar(header, sample, 1000, slow=True)            
                    
                    bu.verbose(header)
                            
                    workers = []
                    for workerID in range(0, N_WORKERS):
                        if stat_type == su.MODE_TANK_STATS:
                            workers.append(asyncio.create_task( check_dup_tank_stat_worker(db, dupsQ, u, workerID, archive )))
                        elif stat_type == su.MODE_PLAYER_ACHIEVEMENTS:
                            workers.append(asyncio.create_task( check_dup_player_achievements_worker(db, dupsQ, u, workerID, archive )))
                    
                    await asyncio.wait([fetcher])
                    for rl_task in await asyncio.gather(*[fetcher]):
                        rl.merge(rl_task)

                    await dupsQ.join()
                    for worker in workers:
                        worker.cancel()
                    for rl_worker in await asyncio.gather(*workers):
                        rl.merge(rl_worker)

                    bu.finish_progress_bar()
                except Exception as err:
                    bu.error(exception=err)

        await update_log(db, 'check', updates, args)

    except Exception as err:
        bu.error(exception=err)
    bu.log(rl.print(do_print=False))
    rl.print()
    return None


def get_mode_str(stat_type: str, archive : bool = False) -> str:
    try:
        ret = su.STR_MODES[stat_type]
        if archive == True:
            return ret + ' (Archive)'
        else:
            return ret
    except Exception as err:
        bu.error(exception=err)
        

async def check_dup_tank_stat_worker(db: motor.motor_asyncio.AsyncIOMotorDatabase, 
                                     dupsQ: asyncio.Queue, update_record: dict = None,
                                     ID: int = 0, archive = False,) -> RecordLogger:
    """Worker to check Tank Stats duplicates. Returns results in a dict"""
    try:
        rl = RecordLogger(get_mode_str(su.MODE_TANK_STATS, archive) + ' duplicates')
        if archive:
            stat_type = su.MODE_TANK_STATS + su.MODE_ARCHIVE
        else:
            stat_type = su.MODE_TANK_STATS    
        dbc = db[su.DB_C[stat_type]]
        
        update_str = su.UPDATE_ALL
        if update_record != None:
            update  = update_record['update']
            start   = update_record['start']
            end     = update_record['end']   
            update_str = update         
        else:
            if archive:
                bu.error('Trying to check duplicates in the whole Archieve. Must define an update.', id=ID)
                rl.log('CRITICAL ERROR')
                return rl
            update = None
            start  = 0
            end    = bu.NOW()

        while True:
            dups = await dupsQ.get()
            if dups['stat_type'] != stat_type:
                bu.error('Invalid stat_type read from duplicate queue: ' + dups['stat_type'])
                dupsQ.task_done()
                continue

            bu.debug(str(len(dups['ids'])) + ' duplicates fetched from queue', id=ID)
            for _id in dups['ids']:
                try:
                    bu.print_progress()
                    dup_stat  = await dbc.find_one({'_id': _id})
                    if dup_stat == None:
                        rl.log('Not Found')
                        bu.error('Could not find duplicate _id=' + str(_id), id=ID)
                        continue
                    last_battle_time= dup_stat['last_battle_time']
                    account_id      = dup_stat['account_id']
                    tank_id         = dup_stat['tank_id']

                    if (update_record != None) and (last_battle_time > end or last_battle_time <= start):
                        bu.verbose('The duplicate is not within update ' +  update_str + '. Skipping')
                        rl.log('Skipped')
                        continue                
                    match = [ {'tank_id': tank_id}, {'account_id': account_id}]
                    if update_record != None:
                        match.append({'last_battle_time': { '$gt': last_battle_time }} )
                        match.append({'last_battle_time': { '$lte': end }} )

                    newer_stat = await dbc.find_one({ '$and': match })
                    if newer_stat == None:
                        rl.log('Invalid')
                        bu.error(str_tank_stat(update, account_id, tank_id, last_battle_time, 'INVALID DUPLICATE: _id=' + str(_id)))                    
                    else:
                        rl.log('OK')
                        bu.verbose(str_tank_stat(update, account_id, tank_id, last_battle_time, 'Analyzed duplicate', pretty_date=True))
                        last_battle_time= newer_stat['last_battle_time']
                        account_id      = newer_stat['account_id']
                        tank_id         = newer_stat['tank_id']
                        bu.verbose(str_tank_stat(update, account_id, tank_id, last_battle_time, 'Newer stat found', pretty_date=True))
                    bu.debug('A duplicate processed', id=ID)
                except Exception as err:
                    rl.log('Errors')
                    bu.error('Error checking ' + get_mode_str(su.MODE_TANK_STATS, archive) + ' duplicates. _id=' + str(_id), err, id=ID)
            dupsQ.task_done()
    
    except asyncio.CancelledError as err:
        bu.debug('Cancelling', id=ID)
    except Exception as err:
        bu.error('Mode=' + stat_type, exception=err, id=ID)

    total = rl.sum(['OK', 'Invalid', 'Skipped'])
    rl.log('Total', total)
    return rl
            

async def check_dup_player_achievements_worker(db: motor.motor_asyncio.AsyncIOMotorDatabase, 
                                               dupsQ: asyncio.Queue, update_record: dict = None,
                                               ID: int = 0, archive = False,) -> RecordLogger:
    """Worker to check Player Achivement duplicates. Returns results in a dict"""
    try:
        
        rl = RecordLogger(get_mode_str(su.MODE_PLAYER_ACHIEVEMENTS, archive))
        if archive:
            stat_type = su.MODE_PLAYER_ACHIEVEMENTS + su.MODE_ARCHIVE
        else:
            stat_type   = su.MODE_PLAYER_ACHIEVEMENTS

        dbc = db[su.DB_C[stat_type]]
        
        if update_record != None:
            update  = update_record['update']
            start   = update_record['start']
            end     = update_record['end']            
        else:
            if archive:
                bu.error('Trying to check duplicates in the whole Archieve. Must define an update.', id=ID)
                rl.log('CRITICAL ERROR')
                return rl
            update = None
            start  = 0
            end    = bu.NOW()           

        while True:
            dups = await dupsQ.get()
            if dups['stat_type'] != stat_type:
                bu.error('Invalid stat_type read from duplicate queue: ' + dups['stat_type'])
                dupsQ.task_done()
                continue

            bu.debug('Duplicate candidate read from the queue', id=ID)
            for _id in dups['ids']:
                try:
                    bu.debug('Checking _id=' + str(_id), id=ID)
                    bu.print_progress()
                    dup_stat  = await dbc.find_one({'_id': _id})
                    if dup_stat == None:
                        rl.log('Not Found')
                        bu.error('Could not find duplicate _id=' + str(_id), id=ID)
                        continue

                    updated         = dup_stat['updated']
                    account_id      = dup_stat['account_id']
                    
                    if (update_record != None) and (updated > end or updated <= start):
                        bu.verbose('The duplicate is not within update ' +  update + '. Skipping')
                        rl.log('Skipped')
                        continue
                
                    newer_stat = await dbc.find_one({ '$and': [ {'account_id': account_id},
                                                                {'updated': { '$gt': updated }}, 
                                                                {'updated': { '$lte': end }}
                                                                ] })
                    if newer_stat == None:
                        rl.log('Invalid')
                        bu.error(str_dups_player_achievements(update, account_id, updated, status='INVALID DUPLICATE: _id=' + str(_id))) 
                    else:
                        rl.log('OK')
                        if bu.is_verbose():
                            bu.verbose('-------------------------------------------------')
                            bu.verbose(str_dups_player_achievements(update, account_id, updated, 'Analyzed duplicate', pretty_date=True))
                            updated         = newer_stat['updated']
                            account_id      = newer_stat['account_id']                    
                            bu.verbose(str_dups_player_achievements(update, account_id, updated, 'Newer stat found', pretty_date=True))

                except Exception as err:
                    rl.log('Errors')
                    bu.error('Error checking ' + get_mode_str(su.MODE_PLAYER_ACHIEVEMENTS, archive) + ' duplicates. _id=' + str(_id), err, id=ID)
            dupsQ.task_done()
    
    except asyncio.CancelledError as err:
        bu.debug('Cancelling', id=ID)
    except Exception as err:
        bu.error('Mode=' + stat_type, err)

    total = rl.sum(['OK', 'Invalid', 'Skipped'])
    rl.log('Total', total)
    return rl
            


def split_int(total:int, N: int) -> list:
    try:
        res = list()
        if N == None or N <= 0 or N > total:
            bu.debug('Invalid argument N')
            return res
        left = total
        for _ in range(N-1):
            sub_total = int(total/N) 
            res.append(sub_total)
            left -= sub_total
        res.append(left)    
    except Exception as err:
        bu.error(exception=err)
    return res


# def print_dups_stats(stat_type: str, dups_total: int, sample: int, dups_ok: int = 0, dups_nok: int = 0, dups_skipped: int= 0):
#     try:
#         sample_str = (str(sample)  +  " (" + '{:.2f}'.format(sample/dups_total*100) + "%)") if sample > 0 else "all"        
#         bu.verbose_std('Total ' + str(dups_total) + ' ' + stat_type +' duplicates. Checked ' + sample_str + " duplicates, skipped " + str(dups_skipped))
#         bu.verbose_std("OK: " + str(dups_ok) + " Errors: " + str(dups_nok))
#         return dups_nok == 0
#     except Exception as err:
#         bu.error(exception=err)


async def find_update(db: motor.motor_asyncio.AsyncIOMotorDatabase, updates : list = None, time: int = -1):
    try:
        if updates == None:
            updates = mk_update_list(db, [ "6.0+" ])
        update = None
        for u in reversed(updates):
            # find the correct update
            if time  > u['start'] and time <= u['end']:
                update  = u                
                break
        return update
    except Exception as err:
        bu.error(exception=err)   


def str_dups_player_achievements(update : str, account_id: int,  updated: int, status: str = None, pretty_date=False):
    """Return string of duplicate status of player achivement stat"""
    try:
        if status == None:
            status = ''    
        if pretty_date:
            return('Update: {:s} account_id={:<10d} updated={:d} ({:s}): {:s}'.format(update, account_id, updated, time.strftime("%Y-%m-%d %H:%M", time.gmtime(updated)), status) )
        else:
            return('Update: {:s} account_id={:<10d} updated={:d} : {:s}'.format(update, account_id, updated, status) )
    except Exception as err:
        bu.error(exception=err)
        return "ERROR"


def str_tank_stat(update : str, account_id: int, tank_id: int, last_battle_time: int, status: str = None, pretty_date=False):
    try:
        if status == None:
            status = ''        
        if pretty_date:
            return('Update: {:s} account_id={:<10d} tank_id={:<5d} latest_battle_time={:d} ({:s}): {:s}'.format(update, account_id, tank_id, last_battle_time, time.strftime("%Y-%m-%d %H:%M", time.gmtime(last_battle_time)),status) )
        else:
            return('Update: {:s} account_id={:<10d} tank_id={:<5d} latest_battle_time={:d} : {:s}'.format(update, account_id, tank_id, last_battle_time, status) )
    except Exception as err:
        bu.error(exception=err)
        return "ERROR"


async def add_stat2del(db: motor.motor_asyncio.AsyncIOMotorDatabase, stat_type: str, id: str, workerID: int = None,  prune : bool = False) -> int:
    """Adds _id of the stat record to be deleted in into su.DB_C_STATS_2_DEL"""
    dbc = db[su.DB_C_STATS_2_DEL]
    dbc2prune = db[su.DB_C[stat_type]]
    try:
        if prune:
            res = await dbc2prune.delete_one( { '_id': id } )
            return res.deleted_count
        else:
            await dbc.insert_one({'type': stat_type, 'id': id})
            return 1
    except Exception as err:
        bu.error(exception=err, id=workerID)
    return 0


async def count_dups2prune(db: motor.motor_asyncio.AsyncIOMotorDatabase, stat_type:str, archive: bool = False, update: str = None) -> int:
    try:
        dbc = db[su.DB_C_STATS_2_DEL]
        if archive:
            stat_type = stat_type + su.MODE_ARCHIVE
        if update != None:
            return await dbc.count_documents({ '$and': [ {'type' : stat_type}, { 'update': update} ] } )
        else:
            return await dbc.count_documents({'type' : stat_type})
    except Exception as err:
        bu.error(exception=err)
    return None


async def prune_stats(db: motor.motor_asyncio.AsyncIOMotorDatabase, 
                      updates: list = list(), 
                      args : argparse.Namespace = None, 
                      stat_type: str = None, 
                      pruneQ: asyncio.Queue = None):
    """Parellen DB pruning, DELETES DATA. Does NOT verify whether there are newer stats"""
    try:
        rl = RecordLogger('Prune')
        archive = args.opt_archive
        sample = args.sample

        if stat_type == None:
            modes = args.mode
        else:
            modes = [ stat_type ]

        for u in updates: 
            for stat_type in modes:
                try:
                    if u == None:
                        if archive:
                            bu.error('Pruning the archive requires an defined update')
                            sys.exit(1)                    
                        update = None
                        bu.verbose_std('PRUNING ' + get_mode_str(stat_type, archive) + ' for duplicates. ALL updates.')
                    else:
                        update = u['update']
                        bu.verbose_std('PRUNING ' + get_mode_str(stat_type, archive) + ' for duplicates. Update ' + update)
                
                    N_stats2prune = await count_dups2prune(db, stat_type, archive, update)
                    N_stats2prune = min(sample, N_stats2prune) if (sample != None) and (sample > 0) else N_stats2prune 
                    bu.debug('Pruning ' + str(N_stats2prune) + ' ' + get_mode_str(stat_type, archive))            
                    bu.set_progress_bar(get_mode_str(stat_type, archive) + ' pruned', N_stats2prune, step = 1000, slow=True)
                    pruneQ = asyncio.Queue(QUEUE_LEN)
                    fetcher = asyncio.create_task(get_dups_worker(db, stat_type, pruneQ, sample, archive=archive, update=update))
                    workers = list()
                    for workerID in range(0, N_WORKERS):
                        workers.append(asyncio.create_task(prune_stats_worker(db, stat_type, pruneQ, u , workerID, archive=archive)))                    

                    await asyncio.wait([fetcher])
                    for rl_task in await asyncio.gather(*[fetcher], return_exceptions=True):
                        rl.merge(rl_task)
                
                    await pruneQ.join()
                    if len(workers) > 0:
                        for worker in workers:
                            worker.cancel()
                        for res_rl in await asyncio.gather(*workers):
                            rl.merge(res_rl)
                    bu.finish_progress_bar()
                except Exception as err:
                    bu.error(exception=err)
        await update_log(db, 'prune', updates, args, modes)
    except Exception as err:
        bu.error(exception=err)
    bu.log(rl.print(do_print=False))
    rl.print()
    return None


async def prune_stats_worker(db: motor.motor_asyncio.AsyncIOMotorDatabase, 
                             stat_type : str, 
                             pruneQ: asyncio.Queue, 
                             update_record: dict = None, 
                             ID: int = 0, 
                             archive: bool = False, 
                             check: bool = False) -> dict:
    """Paraller Worker for pruning stats"""    
    try:
        bu.debug('Started', id=ID)
        rl              = RecordLogger(get_mode_str(stat_type, archive))
        dbc_prunelist   = db[su.DB_C_STATS_2_DEL]       
        if archive:
            stat_type   = stat_type + su.MODE_ARCHIVE
            dbc_check   = None
        else:
            dbc_check   = db[su.DB_C_ARCHIVE[stat_type]]
        dbc_2_prune = db[su.DB_C[stat_type]]

        if update_record != None:
            update  = update_record['update']
            start   = update_record['start']
            end     = update_record['end']            
        else:
            if archive:
                bu.error('Trying to check duplicates in the whole Archieve. Must define an update.', id=ID)
                rl.log('CRITICAL ERROR')
                return rl
            update = None
            start  = 0
            end    = bu.NOW()
        #timer = Timer('Execution time', ['Active', 'Prune DB', 'Prunelist DB'], start=True)
        while True:
            # timer.stop(['Active', 'Prune DB', 'Prunelist DB'])
            prune_task  = await pruneQ.get()
            if prune_task['stat_type'] != stat_type:
                pruneQ.task_done()
                bu.error('Received wrong stat_type from prune queue: ' + prune_task['stat_type'])
                continue

            # timer.start(['Active', 'Prune DB'])
            try:
                ids     = prune_task['ids']
                from_db = prune_task['from_db']
                # task_update = prune_task['update']
                
                if check and dbc_check != None:
                    cursor = dbc_check.find( {'_id': { '$in': ids }}) 
                    res = await cursor.to_list(DEFAULT_BATCH)
                    if len(ids) != len(res):
                        bu.error('Not all stats to be pruned can be found from ' + get_mode_str(stat_type, True))
                        rl.log('Archive check failed', len(ids))
                        pruneQ.task_done()
                        continue

                if update != None:
                    if stat_type in [su.MODE_TANK_STATS , su.MODE_TANK_STATS + su.MODE_ARCHIVE]:
                        res = await dbc_2_prune.delete_many({ '$and': [ { '_id': { '$in':  ids} }, 
                                                            { 'last_battle_time': { '$gt': start }} , 
                                                            { 'last_battle_time': { '$lte': end }} ] })
                    elif stat_type in [ su.MODE_PLAYER_ACHIEVEMENTS, su.MODE_PLAYER_ACHIEVEMENTS + su.MODE_ARCHIVE]:
                        res = await dbc_2_prune.delete_many({ '$and': [ { '_id': { '$in':  ids} }, 
                                                            { 'updated': { '$gt': start }} , 
                                                            { 'updated': { '$lte': end }} ] })
                    else:
                        bu.error('Unsupported stat_type')
                        sys.exit(1)
                else:
                    res = await dbc_2_prune.delete_many( { '_id': { '$in': ids } } )
                # timer.stop(['Prune DB'])
                # timer.start(['Prunelist DB'])
                not_deleted = len(ids) - res.deleted_count
                rl.log('Pruned', res.deleted_count)
                bu.print_progress(res.deleted_count)
                if not_deleted != 0:
                    cursor = dbc_2_prune.find({ '_id': { '$in': ids }}, { '_id': True } ) 
                    docs_not_deleted = set( await cursor.to_list(DEFAULT_BATCH))
                    still_in_db = len(docs_not_deleted)
                    docs_deleted = list(set(ids) - docs_not_deleted)
                    bu.debug('Could not prune all ' + get_mode_str(stat_type, archive) + ': pruned=' + str(res.deleted_count) + ' NOT pruned=' + str(still_in_db) + ' attempted, but not found=' + str(not_deleted - still_in_db))
                    rl.log('NOT pruned', still_in_db)
                    rl.log('Not found', not_deleted - still_in_db)
                else:
                    docs_deleted = ids

                if from_db:
                    await dbc_prunelist.delete_many({ '$and': [ {'id': { '$in': docs_deleted }}, {'type': stat_type} ]})

            except Exception as err:
                bu.error(exception=err, id=ID)
                rl.log('Error')
            
            pruneQ.task_done()        # is executed even when 'continue' is called

    except (asyncio.CancelledError):
        bu.debug('Prune queue is empty', id=ID)
    except Exception as err:
        bu.error(exception=err, id=ID)
    # timer.stop(all=True)
    # for mode in timer.get_modes():
    #     rl.log(timer.get_mode_name(mode), timer.elapsed(mode))    
    return rl


async def get_tanks_DB(db: motor.motor_asyncio.AsyncIOMotorDatabase, archive=False) -> list:
    """Get tank_ids of tanks in the DB"""
    try:
        if archive: 
            collection = su.DB_C_ARCHIVE[su.MODE_TANK_STATS]
        else:
            collection = su.DB_C[su.MODE_TANK_STATS]
        dbc = db[collection]
        return sorted(await dbc.distinct('tank_id'))
    except Exception as err:
        bu.error('Could not fetch tank_ids', exception=err)
    return None


async def get_tank_name(db: motor.motor_asyncio.AsyncIOMotorDatabase, tank_id: int) -> str:
    """Get tank name from DB's Tankopedia"""
    try:
        dbc = db[su.DB_C_TANKS]
        res = await dbc.find_one( { 'tank_id': int(tank_id)}, { '_id': 0, 'name': 1} )
        return res['name']
    except Exception as err:
        bu.debug(exception=err)
    return None


async def get_tanks_opt(db: motor.motor_asyncio.AsyncIOMotorDatabase, option: list = None, archive=False):
    """read option and return tank_ids"""
    try:
        TANK_ID_MAX = int(10e6)
        tank_id_start = TANK_ID_MAX
        tank_ids = set()
        p = re.compile(r'^(\d+)(\+)?$')
        for tank in option:
            try:
                m = p.match(tank).groups()
                if m[0] == None:
                    raise Exception('Invalid tank_id given' + str(tank))
                if m[1] != None:
                    tank_id_start = min(int(m[0]), tank_id_start)
                else:
                    tank_ids.add(int(m[0]))
            except Exception as err:
                bu.error('Invalid tank_id give: ' + tank, exception=err)        
        if tank_id_start < TANK_ID_MAX:            
            all_tanks = await get_tanks_DB(db, archive)
            tank_ids_start = [ tank_id for tank_id in all_tanks if tank_id >= tank_id_start ]
            tank_ids.update(tank_ids_start)        
        return list(tank_ids)
    except Exception as err:
        bu.error('Returning empty list', exception=err)
    return list()


async def archive_player_achivements(db: motor.motor_asyncio.AsyncIOMotorDatabase, args: argparse.Namespace = None):
    try:
        stat_type   = su.MODE_PLAYER_ACHIEVEMENTS
        dbc         = db[su.DB_C[stat_type]]
        dbc_archive = su.DB_C_ARCHIVE[stat_type]
        sample      = args.sample
        rl = RecordLogger('Archive Player Achievements')

        pipeline = [ {'$match': { su.FIELD_NEW_STATS : { '$exists': True } } } ]
        if sample != None:
            pipeline.append( { '$sample': { 'size': sample }})
        else:        
            sample = await dbc.count_documents({ su.FIELD_NEW_STATS : { '$exists': True } })
        
        bu.set_progress_bar('Archiving ' + get_mode_str(stat_type), sample, step = 250, slow=True )  ## After MongoDB fixes $merge cursor: https://jira.mongodb.org/browse/DRIVERS-671
        pipeline.append({ '$unset': su.FIELD_NEW_STATS })
        pipeline.append({ '$merge': { 'into': dbc_archive, 'on': '_id', 'whenMatched': 'keepExisting' }})
        cursor = dbc.aggregate(pipeline, allowDiskUse=True)
        s = 0
        async for _ in cursor:      ## This one does not work yet until MongoDB fixes $merge cursor: https://jira.mongodb.org/browse/DRIVERS-671
            bu.print_progress()
            s +=1
        rl.log('Updated stats found', sample)
        rl.log('Archived', s)   # does not work until MongoDB fixes $merge cursor

        ## Clean the latest stats # TO DO  
        
    except Exception as err:
        bu.error(exception=err)
    finally:
        bu.finish_progress_bar()        
        bu.log(rl.print(do_print=False))
        rl.print()
    return None


async def archive_tank_stats(db: motor.motor_asyncio.AsyncIOMotorDatabase):
    try:
        stat_type   = su.MODE_TANK_STATS
        rl = RecordLogger('Archive Tank stats')
        tankQ = await mk_tankQ(db)        
        #N_stats = await dbc.count_documents({ su.FIELD_NEW_STATS : { '$exists': True } })
        bu.set_progress_bar('Archiving ' + get_mode_str(stat_type), tankQ.qsize(), step = 1, slow=True )
        
        workers = []
        for workerID in range(0, N_WORKERS):
            workers.append(asyncio.create_task(archive_tank_stats_worker(db, tankQ, workerID)))
        
        await tankQ.join()
        bu.finish_progress_bar()
        bu.debug('Waiting for workers to finish')
        if len(workers) > 0:
            i = 0
            for rl_worker in await asyncio.gather(*workers, return_exceptions=True):
                bu.debug('Merging archive_tank_stats_worker\'s RecordLogger', id=i)
                rl.merge(rl_worker)
                i = +1  

        ## Clean the latest stats # TO DO  
        
    except Exception as err:
        bu.error(exception=err)
    finally:
        bu.finish_progress_bar()        
        bu.log(rl.print(do_print=False))
        rl.print()
    return None


async def archive_tank_stats_worker(db: motor.motor_asyncio.AsyncIOMotorDatabase, tankQ: asyncio.Queue, ID: int = 0):
    """Worker for archieving tank stats"""
    stat_type   = su.MODE_TANK_STATS
    dbc         = db[su.DB_C[stat_type]]
    dbc_archive = su.DB_C_ARCHIVE[stat_type]
    rl = RecordLogger('Tank stats')
    while not tankQ.empty():
        tank_id = await tankQ.get()
        try:
            match = { '$and': [  { 'tank_id': tank_id }, { su.FIELD_NEW_STATS : { '$exists': True } } ] }
            pipeline = [ {'$match': match } ]
            pipeline.append({ '$unset': su.FIELD_NEW_STATS })
            pipeline.append({ '$merge': { 'into': dbc_archive, 'on': '_id', 'whenMatched': 'keepExisting' }})
            cursor = dbc.aggregate(pipeline, allowDiskUse=True)
            s = 0
            async for _ in cursor:      ## This one does not work yet until MongoDB fixes $merge cursor: https://jira.mongodb.org/browse/DRIVERS-671
                # bu.print_progress()
                s +=1                
            rl.log('Archived', s)
            rl.log('Tanks processed') 
        except Exception as err:
            bu.error(exception=err, id=ID)
        finally:
            bu.print_progress()
            tankQ.task_done()
    return rl


async def clean_tank_stats(db: motor.motor_asyncio.AsyncIOMotorDatabase):
    """Clean the Latest stats from older stats"""
    try: 
        dbc      = db[su.DB_C[su.MODE_TANK_STATS]]
        rl       = RecordLogger('Clean tank stats')
        q_dirty  = {su.FIELD_NEW_STATS: { '$exists': True}}
        n_dirty  = await dbc.count_documents(q_dirty)
        
        bu.set_progress_bar('Finding stats to clean', n_dirty, slow=True)
        account_tankQ = await mk_account_tankQ(db)
        pruneQ  = asyncio.Queue(QUEUE_LEN)

        workers = list()
        scanners = list()
        for workerID in range(0,N_WORKERS):
            scanners.append(asyncio.create_task(find_dup_tank_stats_worker(db, account_tankQ, pruneQ, None, workerID, archive=False)))
            workers.append(asyncio.create_task(prune_stats_worker(db, su.MODE_TANK_STATS, pruneQ, workerID, check=True)))        

        bu.debug('Waiting for workers to finish')
        await account_tankQ.join()
        if len(scanners) > 0:
            for res in await asyncio.gather(*scanners, return_exceptions=True):
                rl.merge(res)
        
        await pruneQ.join()
        bu.debug('Cancelling workers')
        for worker in workers:
            worker.cancel()
        if len(workers) > 0:
            for res in await asyncio.gather(*workers, return_exceptions=True):
                rl.merge(res)          
       
    except Exception as err:
        bu.error(exception=err)
    finally:
        bu.finish_progress_bar()
        rl.print()
        return rl
    

async def find_dup_tank_stats_worker(  db: motor.motor_asyncio.AsyncIOMotorDatabase, 
                                        account_tankQ: asyncio.Queue, dupsQ: asyncio.Queue, 
                                        update_record: dict = None, ID: int = 0, archive = False) -> RecordLogger:
    """Worker to find duplicates to prune"""
    try:
        rl       = RecordLogger('Find tank stat duplicates')
        update = None
        if archive:            
            stat_type = su.MODE_TANK_STATS + su.MODE_ARCHIVE
        else:            
            stat_type = su.MODE_TANK_STATS
        dbc = db[su.DB_C[stat_type]]
        
        if update_record != None:
            update  = update_record['update']
            start   = update_record['start']
            end     = update_record['end']        
        elif archive:
            bu.error('CRITICAL !!!! TRYING TO PRUNE OLD TANK STATS FROM ARCHIVE !!!! EXITING...')
            sys.exit(1)           

        while not account_tankQ.empty():
            try:
                wp = await account_tankQ.get()
                match_stage = list()
                if 'tank_id' in wp:
                    tank_id  = wp['tank_id']
                    match_stage.append({ 'tank_id': tank_id })
                    group_by = '$account_id'
                else:
                    group_by = '$tank_id'

                if 'account_id' in wp:
                    account_id = wp['account_id']
                    match_stage.append({'account_id': account_id })
                    # bu.debug('tank_id=' + str(tank_id) + ' account_id=' + str(account_id), id=ID)
                else:
                    if group_by != '$account_id':
                        bu.error('CRITICAL ERROR: Wrong arguments given')
                        rl.log('CRITICAL ERROR')
                        return rl
                    account_id_min = wp['account_id_min']
                    account_id_max = wp['account_id_max']
                    match_stage.append({'account_id': { '$gte': account_id_min}})
                    match_stage.append({'account_id': { '$lt': account_id_max}})
                    #bu.debug('tank_id=' + str(tank_id) + ' account_id=' + str(account_id_min) + '-' + str(account_id_max), id=ID)
                
                if update_record != None:
                    match_stage.append( {'last_battle_time': {'$gt': start}} )
                    match_stage.append( {'last_battle_time': {'$lte': end}} )

                pipeline = [{ '$match': { '$and': match_stage } }, 
                            { '$sort': { 'last_battle_time': pymongo.DESCENDING } }, 
                            { '$group': { '_id': group_by, 
                                            'all_ids': {'$push': '$_id' },
                                            'len': { "$sum": 1 } } },                           
                            { '$match': { 'len': { '$gt': 1 } } }, 
                            { '$project': { 'ids': {  '$slice': [  '$all_ids', 1, '$len' ] } } }
                        ]
                cursor = dbc.aggregate(pipeline, allowDiskUse=True)
                async for res in cursor:
                    await dupsQ.put(mk_dups_Q_entry(res['ids'], update=update, stat_type=stat_type))
                    rl.log('Found', len(res['ids']))
                bu.print_progress()                    

            except Exception as err:
                if update != None:
                    bu.error('Update=' + update, exception=err)
                else:
                    bu.error('Update=ALL', exception=err)
            account_tankQ.task_done()

    except Exception as err:
        bu.error(exception=err)
    return rl    


async def find_dup_player_achivements_worker(db: motor.motor_asyncio.AsyncIOMotorDatabase, 
                                        accountQ: asyncio.Queue, dupsQ: asyncio.Queue, 
                                        update_record: dict = None, ID: int = 0, 
                                        archive = False) -> RecordLogger:
    """Worker to find player achivement duplicates to prune"""
    try:
        rl       = RecordLogger('Find player achivement duplicates')
        update = None
        if archive:            
            stat_type = su.MODE_PLAYER_ACHIEVEMENTS + su.MODE_ARCHIVE
        else:
            stat_type = su.MODE_PLAYER_ACHIEVEMENTS
        dbc = db[su.DB_C[stat_type]]

        if update_record != None:
            update  = update_record['update']
            start   = update_record['start']
            end     = update_record['end']        
        elif archive:
            bu.error('CRITICAL !!!! TRYING TO PRUNE --ALL-- OLD TANK STATS FROM ARCHIVE !!!! EXITING...')
            sys.exit(1)
            
        while not accountQ.empty():
            accounts = await accountQ.get()
            try:
                match_stage = [ {'account_id': { '$gte': accounts['min']}}, 
                                {'account_id': { '$lt' : accounts['max'] }} ]
                if update_record != None:
                    match_stage.append( {'updated': {'$gt': start}} )
                    match_stage.append( {'updated': {'$lte': end}} )

                pipeline = [{ '$match': { '$and': match_stage } }, 
                            { '$sort': { 'updated': pymongo.DESCENDING } }, 
                            { '$group': { '_id': '$account_id', 
                                            'all_ids': {'$push': '$_id' },
                                            'len': { "$sum": 1 } } },                           
                            { '$match': { 'len': { '$gt': 1 } } }, 
                            { '$project': { 'ids': {  '$slice': [  '$all_ids', 1, '$len' ] } } }
                        ]
                cursor = dbc.aggregate(pipeline, allowDiskUse=True)
                
                async for res in cursor:
                    # rl.log('Found', len(res['ids']))
                    rl.log('Found')
                    await dupsQ.put(mk_dups_Q_entry(res['ids'], update=update, stat_type=stat_type))
                bu.print_progress()
            except Exception as err:
                if update != None:
                    bu.error('Update=' + update, exception=err)
                else:
                    bu.error('Update=ALL', exception=err)
            finally:
                accountQ.task_done()

    except Exception as err:
        bu.error(exception=err)
    # rl.print()
    return rl  


async def snapshot_player_achivements(db: motor.motor_asyncio.AsyncIOMotorDatabase, args: argparse.Namespace = None):
    try:
        bu.verbose_std('Creating a snapshot of the latest player achievements')
        rl = RecordLogger('Snapshot')
        
        accountQ = await mk_account_rangeQ()
        bu.set_progress_bar('Stats processed', accountQ.qsize(), step=2, slow=True)  
        workers = list()
        for workerID in range(0, N_WORKERS):
            workers.append(asyncio.create_task(snapshot_player_achivements_worker(db, accountQ, workerID)))

        await accountQ.join()

        bu.finish_progress_bar()
        for rl_worker in await asyncio.gather(*workers):
            rl.merge(rl_worker)

    except Exception as err:
        bu.error(exception=err)
    bu.log(rl.print(do_print=False))
    rl.print()
    return None


async def snapshot_player_achivements_worker(db: motor.motor_asyncio.AsyncIOMotorDatabase, 
                                             accountQ: asyncio.Queue, ID: int = 0 ) -> RecordLogger:  
    """Worker to snapshot tank stats"""  
    try:
        target_collection = su.DB_C_PLAYER_ACHIVEMENTS
        dbc_archive       = db[su.DB_C_ARCHIVE[su.MODE_PLAYER_ACHIEVEMENTS]]
        rl                = RecordLogger(get_mode_str(su.MODE_PLAYER_ACHIEVEMENTS))

        while not accountQ.empty():
            try:
                ## FIX: use accounts collection? 
                wp = await accountQ.get()
                account_id_min = wp['min']
                account_id_max = wp['max']
                
                pipeline = [ {'$match': { '$and': [  {'account_id': {'$gte': account_id_min}}, {'account_id': {'$lt': account_id_max}} ] }},
                             {'$sort': {'updated': pymongo.DESCENDING}},
                             {'$group': { '_id': '$account_id',
                                          'doc': {'$first': '$$ROOT'}}},
                             {'$replaceRoot': {'newRoot': '$doc'}}, 
                             {'$merge': { 'into': target_collection, 'on': '_id', 'whenMatched': 'keepExisting' }} ]
                cursor = dbc_archive.aggregate(pipeline, allowDiskUse=True)
                # s = 0
                async for _ in cursor:      
                    # s += 1   ## This one does not work yet until MongoDB fixes $merge cursor: https://jira.mongodb.org/browse/DRIVERS-671
                    pass
                n = await dbc_archive.count_documents({'$match': { '$and': [  {'account_id': {'$gte': account_id_min}}, {'account_id': {'$lt': account_id_max}} ] }} )                
                rl.log('Snapshotted', n)
                bu.debug('account_id range: ' + str(account_id_min) + '-' + str(account_id_max) + ' processed', id=ID)
                bu.print_progress()
            except Exception as err:
                bu.error(exception=err, id=ID)
            accountQ.task_done()

    except Exception as err:
        bu.error(exception=err, id=ID)
    return rl


async def snapshot_tank_stats(db: motor.motor_asyncio.AsyncIOMotorDatabase, args: argparse.Namespace = None):
    try:
        bu.verbose_std('Creating a snapshot of the latest tank stats')
        rl = RecordLogger('Snapshot')
        if args.opt_tanks != None:
            tank_ids = await get_tanks_opt(db, args.opt_tanks, archive=True)
        else:
            tank_ids = None
        account_tankQ = await mk_account_tankQ(db, tank_ids, archive=True)
        bu.set_progress_bar('Stats processed', account_tankQ.qsize(), step=50, slow=True)  
        workers = list()
        for workerID in range(0, N_WORKERS):
            workers.append(asyncio.create_task(snapshot_tank_stats_worker(db, account_tankQ, workerID)))

        await account_tankQ.join()

        bu.finish_progress_bar()
        for rl_worker in await asyncio.gather(*workers):
            rl.merge(rl_worker)

    except Exception as err:
        bu.error(exception=err)
    bu.log(rl.print(do_print=False))
    rl.print()
    return None
    

async def snapshot_tank_stats_worker(db: motor.motor_asyncio.AsyncIOMotorDatabase, 
                                     account_tankQ: asyncio.Queue, ID: int = 0 ) -> RecordLogger:  
    """Worker to snapshot tank stats"""  
    try:
        target_collection = su.DB_C_TANK_STATS
        dbc_archive       = db[su.DB_C_ARCHIVE[su.MODE_TANK_STATS]]
        rl                = RecordLogger(get_mode_str(su.MODE_TANK_STATS))

        while not account_tankQ.empty():
            try:
                wp = await account_tankQ.get()
                account_id_min = wp['account_id_min']
                account_id_max = wp['account_id_max']
                tank_id        = wp['tank_id']

                if bu.is_verbose(True):                                        
                    tank_name = await get_tank_name(db, tank_id)
                    if tank_name == None:
                        tank_name = 'Tank name not found'                    
                    info_str = 'Processing tank (' + tank_name + ' (' +  str(tank_id) + '):'
                    bu.log(info_str)
                
                pipeline = [ {'$match': { '$and': [ {'tank_id': tank_id }, 
                                                    {'account_id': {'$gte': account_id_min}}, 
                                                    {'account_id': {'$lt': account_id_max}} 
                                                ] }},
                             {'$sort': {'last_battle_time': pymongo.DESCENDING}},
                             {'$group': { '_id': '$account_id',
                                          'doc': {'$first': '$$ROOT'}}},
                             {'$replaceRoot': {'newRoot': '$doc'}}, 
                             {'$merge': { 'into': target_collection, 'on': '_id', 'whenMatched': 'keepExisting' }} ]
                cursor = dbc_archive.aggregate(pipeline, allowDiskUse=True)
                # s = 0
                async for _ in cursor:      
                    # s += 1   ## This one does not work yet until MongoDB fixes $merge cursor: https://jira.mongodb.org/browse/DRIVERS-671
                    pass
                # rl.log('Snapshotted', s)
                n = await dbc_archive.count_documents({'$match': { '$and': [ {'tank_id': tank_id }, {'account_id': {'$gte': account_id_min}}, {'account_id': {'$lt': account_id_max}} ] }} )                
                rl.log('Snapshotted', n)
                bu.print_progress()
            except Exception as err:
                bu.error(exception=err, id=ID)
                rl.log('Error')
            account_tankQ.task_done()

    except Exception as err:
        bu.error(exception=err, id=ID)
    return rl


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


# main()
if __name__ == "__main__":
    #asyncio.run(main(sys.argv[1:]), debug=True)
    asyncio.run(main(sys.argv[1:]))
