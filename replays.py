from argparse import ArgumentParser, Namespace
from configparser import ConfigParser
from typing import Optional, cast, Iterable, Any
import logging
from asyncio import create_task, gather, Queue, CancelledError, Task
#from aiohttp import ClientResponse
from os.path import isfile

from backend import Backend, BSTableType, get_sub_type

from pyutils import get_url, get_url_JSON_model, epoch_now, EventCounter, \
					JSONExportable, is_alphanum
from blitzutils.models import WoTBlitzReplayJSON, Region
from blitzutils.wotinspector import WoTinspector

logger = logging.getLogger()
error 	= logger.error
message	= logger.warning
verbose	= logger.info
debug	= logger.debug

WI_MAX_PAGES 	: int 				= 100
WI_MAX_OLD_REPLAYS: int 			= 30
WI_RATE_LIMIT	: Optional[float] 	= 20/3600
WI_AUTH_TOKEN	: Optional[str] 	= None
REPLAY_Q_MAX : int 					= 500
# ACCOUNTS_Q_MAX 	: int				= 100
# ACCOUNT_Q_MAX 	: int				= 5000

###########################################
# 
# add_args_accouts functions  
#
###########################################


def add_args(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	try:		
		replays_parsers = parser.add_subparsers(dest='replays_cmd', 	
												title='replays commands',
												description='valid commands',
												help='replays help',
												metavar='export')
		replays_parsers.required = True
		export_parser = replays_parsers.add_parser('export', help="replays export help")
		if not add_args_export(export_parser, config=config):
			raise Exception("Failed to define argument parser for: replays export")

		import_parser = replays_parsers.add_parser('import', help="replays import help")
		if not add_args_import(import_parser, config=config):
			raise Exception("Failed to define argument parser for: replays import")		
				
		return True
	except Exception as err:
		error(f'add_args(): {err}')
	return False


## -

def add_args_export(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	try:
		parser.add_argument('--file', action='store_true', default=False, 
							dest='replay_export_file', 
							help='Export replay(s) to file(s)')
		replays_export_parsers = parser.add_subparsers(dest='replays_export_query_type', 	
														title='replays export query-type',
														description='valid query-types', 
														metavar='id')
		replays_export_parsers.required = True
		

		replays_export_id_parser = replays_export_parsers.add_parser('id', help='replays export id help')
		if not add_args_export_id(replays_export_id_parser, config=config):
			raise Exception("Failed to define argument parser for: replays export id")
		
		## Not implemented yet
		# replays_export_find_parser = replays_export_parsers.add_parser('find', help='replays export find help')
		# if not add_args_export_find(replays_export_find_parser, config=config):
		# 	raise Exception("Failed to define argument parser for: replays export find")		
		
		return True	
	except Exception as err:
		error(f'add_args_export() : {err}')
	return False


def add_args_export_id(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	"""Add argparse arguments for replays export id -subcommand"""
	try:
		parser.add_argument('replay_export_id',type=str, metavar='REPLAY-ID', help='Replay ID to export')		
		return True
	except Exception as err:
		error(f'add_args_export_id() : {err}')
	return False


def add_args_export_find(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	"""Add argparse arguments for replays export find -subcommand"""
	## NOT IMPLEMENTED
	try:
		return True
	except Exception as err:
		error(f'add_args_export_find() : {err}')
	return False


def add_args_import(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	try:
		import_parsers = parser.add_subparsers(dest='import_backend', 	
														title='replays import backend',
														description='valid import backends', 
														metavar=' | '.join(Backend.list_available()))
		import_parsers.required = True
		
		for backend in Backend.get_registered():
			import_parser =  import_parsers.add_parser(backend.driver, help=f'replays import {backend.driver} help')
			if not backend.add_args_import(import_parser, config=config):
				raise Exception(f'Failed to define argument parser for: replays import {backend.driver}')

		parser.add_argument('--sample', type=float, default=0, help='Sample size')
		parser.add_argument('--force', action='store_true', default=False, 
							help='Overwrite existing file(s) when exporting')
				
		return True	
	except Exception as err:
		error(f'add_args_import() : {err}')
	return False


###########################################
# 
# cmd_accouts functions  
#
###########################################

async def cmd(db: Backend, args : Namespace) -> bool:
	
	try:
		debug('replays')
		
		if args.replays_cmd == 'export':
			if args.replays_export_query_type == 'id':
				debug('export id')
				return await cmd_export_id(db, args)
			elif args.replays_export_query_type == 'find':
				debug('find')
				return await cmd_export_find(db, args)
			else:
				error('replays: unknown or missing subcommand')
		elif args.replays_cmd == 'import':
			return await cmd_import(db, args)

	except Exception as err:
		error(f'{err}')
	return False


async def cmd_export_id(db: Backend, args : Namespace) -> bool:
	try:
		debug('starting')
		id : str = args.replay_export_id
		replay : WoTBlitzReplayJSON | None = await db.replay_get(id)
		if replay is None:
			error('Could not find replay id: {id}')
			return False
		if args.replay_export_file:
			return await cmd_export_files(args, [replay])
		else:
			print(replay.json_src(indent=4))			
		return True 
	except Exception as err:
		error(f'{err}')
	return False


async def cmd_export_files(args: Namespace, replays: Iterable[WoTBlitzReplayJSON]) -> bool:
	raise NotImplementedError
	return False


async def cmd_export_find(db: Backend, args : Namespace) -> bool:
	raise NotImplementedError
	return False


async def  cmd_import(db: Backend, args : Namespace) -> bool:
	"""Import replays from other backend"""	
	try:
		assert is_alphanum(args.import_model), f'invalid --import-model: {args.import_model}'

		stats 		: EventCounter 			= EventCounter('replays import')
		replayQ 	: Queue[WoTBlitzReplayJSON]	= Queue(REPLAY_Q_MAX)
		sample  	: float 				= args.sample
		import_db   	: Backend | None 				= None
		import_backend 	: str 							= args.import_backend
		import_model 	: type[JSONExportable] | None 	= None

		if (import_model := get_sub_type(args.import_model, JSONExportable)) is None:
			raise ValueError("--import-model has to be subclass of JSONExportable")

		importer : Task = create_task(db.replays_insert_worker(replayQ=replayQ, force=args.force))

		if (import_db := Backend.create_import_backend(driver=import_backend, 
														args=args, 
														import_type=BSTableType.Replays, 
														copy_from=db,
														config_file=args.import_config)) is None:
			raise ValueError(f'Could not init {import_backend} to import releases from')

		async for replay in import_db.replays_export(model=import_model, sample=sample):
			await replayQ.put(replay)
			stats.log('read')

		await replayQ.join()
		importer.cancel()
		worker_res : tuple[EventCounter|BaseException] = await gather(importer,return_exceptions=True)
		if type(worker_res[0]) is EventCounter:
			stats.merge_child(worker_res[0])
		elif type(worker_res[0]) is BaseException:
			error(f'replays insert worker threw an exception: {worker_res[0]}')
		stats.print()
		return True
	except Exception as err:
		error(f'{err}')	
	return False