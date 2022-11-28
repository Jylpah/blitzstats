from argparse import ArgumentParser, Namespace
from configparser import ConfigParser
from typing import Optional, cast, Iterable
import logging
from asyncio import create_task, gather, Queue, CancelledError, Task
from aiohttp import ClientResponse

from backend import Backend
from pyutils.eventcounter import EventCounter
from pyutils.utils import get_url, get_url_JSON_model, epoch_now
from blitzutils.models import WoTBlitzReplayJSON, Region
from blitzutils.wotinspector import WoTinspector

logger = logging.getLogger()
error 	= logger.error
message	= logger.warning
verbose	= logger.info
debug	= logger.debug

WI_MAX_PAGES 	: int 				= 100
WI_MAX_OLD_REPLAYS: int 			= 30
WI_RATE_LIMIT	: Optional[float] 	= None
WI_AUTH_TOKEN	: Optional[str] 	= None
ACCOUNTS_Q_MAX 	: int				= 100
ACCOUNT_Q_MAX 	: int				= 5000

###########################################
# 
# add_args_accouts functions  
#
###########################################


def add_args_replays(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	try:		
		replays_parsers = parser.add_subparsers(dest='replays_cmd', 	
												title='replays commands',
												description='valid commands',
												help='replays help',
												metavar='export')
		replays_parsers.required = True
		export_parser = replays_parsers.add_parser('export', help="replays export help")
		if not add_args_replays_export(export_parser, config=config):
			raise Exception("Failed to define argument parser for: replays export")

		import_parser = replays_parsers.add_parser('import', help="replays import help")
		if not add_args_replays_import(import_parser, config=config):
			raise Exception("Failed to define argument parser for: replays import")		
				
		return True
	except Exception as err:
		error(f'add_args_replays(): {err}')
	return False


## -

def add_args_replays_export(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
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
		if not add_args_replays_export_id(replays_export_id_parser, config=config):
			raise Exception("Failed to define argument parser for: replays export id")
		
		## Not implemented yet
		# replays_export_find_parser = replays_export_parsers.add_parser('find', help='replays export find help')
		# if not add_args_replays_export_find(replays_export_find_parser, config=config):
		# 	raise Exception("Failed to define argument parser for: replays export find")		
		
		return True	
	except Exception as err:
		error(f'add_args_replays_export() : {err}')
	return False


def add_args_replays_export_id(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	"""Add argparse arguments for replays export id -subcommand"""
	try:
		parser.add_argument('replay_export_id',type=str, metavar='REPLAY-ID', help='Replay ID to export')		
		return True
	except Exception as err:
		error(f'add_args_replays_export_id() : {err}')
	return False


def add_args_replays_export_find(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	"""Add argparse arguments for replays export find -subcommand"""
	## NOT IMPLEMENTED
	try:
		return True
	except Exception as err:
		error(f'add_args_replays_export_find() : {err}')
	return False


def add_args_replays_import(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	try:
		parser.add_argument('--replace', action='store_true', default=False, 
							dest='replay_import_replace', 
							help='Replace existing documents in the backend with the same primary key')
		# Add selection --id | --all
		replays_import_parsers = parser.add_subparsers(dest='replays_import_source', 	
														title='replays import source',
														description='valid import sources', 
														metavar=' | '.join(Backend.list_available()))
		replays_import_parsers.required = True
		
		replays_import_mongodb_parser = replays_import_parsers.add_parser('mongodb', help='replays import mongodb help')
		if not add_args_replays_import_mongodb(replays_import_mongodb_parser, config=config):
			raise Exception("Failed to define argument parser for: replays import mongodb")
		
		## Not implemented yet
		# replays_export_find_parser = replays_export_parsers.add_parser('find', help='replays export find help')
		# if not add_args_replays_export_find(replays_export_find_parser, config=config):
		# 	raise Exception("Failed to define argument parser for: replays export find")		
		
		return True	
	except Exception as err:
		error(f'add_args_replays_export() : {err}')
	return False


def add_args_replays_import_mongodb(parser: ArgumentParser, config: Optional[ConfigParser] = None) -> bool:
	return True
	raise NotImplementedError


###########################################
# 
# cmd_accouts functions  
#
###########################################

async def cmd_replays(db: Backend, args : Namespace) -> bool:
	
	try:
		debug('replays')
		
		if args.replays_cmd == 'export':
			if args.replays_export_query_type == 'id':
				debug('export id')
				return await cmd_replays_export_id(db, args)
			elif args.replays_export_query_type == 'find':
				debug('find')
				return await cmd_replays_export_find(db, args)
			else:
				error('replays: unknown or missing subcommand')

	except Exception as err:
		error(f'{err}')
	return False


async def cmd_replays_export_id(db: Backend, args : Namespace) -> bool:
	try:
		debug('starting')
		id : str = args.replay_export_id
		replay : WoTBlitzReplayJSON | None = await db.replay_get(id)
		if replay is None:
			error('Could not find replay id: {id}')
			return False
		if args.replay_export_file:
			return await replays_export_files(args, [replay])
		else:
			print(replay.json_src(indent=4))			
		return True 
	except Exception as err:
		error(f'{err}')
	return False


async def replays_export_files(args: Namespace, replays: Iterable[WoTBlitzReplayJSON]) -> bool:
	raise NotImplementedError
	return False

async def cmd_replays_export_find(db: Backend, args : Namespace) -> bool:
	raise NotImplementedError
	return False