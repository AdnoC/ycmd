#!/usr/bin/env python
#
# Copyright (C) 2013  Google Inc.
#
# This file is part of YouCompleteMe.
#
# YouCompleteMe is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# YouCompleteMe is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with YouCompleteMe.  If not, see <http://www.gnu.org/licenses/>.

from os import path

try:
  import ycm_core
except ImportError as e:
  raise RuntimeError(
    'Error importing ycm_core. Are you sure you have placed a '
    'version 3.2+ libclang.[so|dll|dylib] in folder "{0}"? '
    'See the Installation Guide in the docs. Full error: {1}'.format(
      path.realpath( path.join( path.abspath( __file__ ), '../..' ) ),
      str( e ) ) )

import atexit
import logging
import json
import bottle
import httplib
from bottle import request, response
import server_state
from ycmd import user_options_store
from ycmd.responses import BuildExceptionResponse, BuildCompletionResponse
from ycmd import hmac_plugin
from ycmd import extra_conf_store
from ycmd.request_wrap import RequestWrap


# num bytes for the request body buffer; request.json only works if the request
# size is less than this
bottle.Request.MEMFILE_MAX = 1000 * 1024

_server_state = None
_hmac_secret = None
_logger = logging.getLogger( __name__ )
app = bottle.Bottle()


@app.post( '/event_notification' )
def EventNotification():
  _logger.info( 'Received event notification' )
  request_data = RequestWrap( request.json )
  event_name = request_data[ 'event_name' ]
  _logger.debug( 'Event name: %s', event_name )

  event_handler = 'On' + event_name
  getattr( _server_state.GetGeneralCompleter(), event_handler )( request_data )

  filetypes = request_data[ 'filetypes' ]
  response_data = None
  if _server_state.FiletypeCompletionUsable( filetypes ):
    response_data = getattr( _server_state.GetFiletypeCompleter( filetypes ),
                             event_handler )( request_data )

  if response_data:
    return _JsonResponse( response_data )
  return _JsonResponse( {} )


@app.post( '/run_completer_command' )
def RunCompleterCommand():
  _logger.info( 'Received command request' )
  request_data = RequestWrap( request.json )
  completer = _GetCompleterForRequestData( request_data )

  return _JsonResponse( completer.OnUserCommand(
      request_data[ 'command_arguments' ],
      request_data ) )


@app.post( '/completions' )
def GetCompletions():
  _logger.info( 'Received completion request' )
  request_data = RequestWrap( request.json )
  do_filetype_completion = _server_state.ShouldUseFiletypeCompleter(
    request_data )
  _logger.debug( 'Using filetype completion: %s', do_filetype_completion )
  filetypes = request_data[ 'filetypes' ]
  completer = ( _server_state.GetFiletypeCompleter( filetypes ) if
                do_filetype_completion else
                _server_state.GetGeneralCompleter() )

  return _JsonResponse( BuildCompletionResponse(
      completer.ComputeCandidates( request_data ),
      request_data.CompletionStartColumn() ) )


@app.get( '/user_options' )
def GetUserOptions():
  _logger.info( 'Received user options GET request' )
  return _JsonResponse( dict( _server_state.user_options ) )


@app.get( '/healthy' )
def GetHealthy():
  _logger.info( 'Received health request' )
  if request.query.include_subservers:
    cs_completer = _server_state.GetFiletypeCompleter( ['cs'] )
    return _JsonResponse( cs_completer.ServerIsRunning() )
  return _JsonResponse( True )


@app.get( '/ready' )
def GetReady():
  _logger.info( 'Received ready request' )
  if request.query.include_subservers:
    cs_completer = _server_state.GetFiletypeCompleter( ['cs'] )
    return _JsonResponse( cs_completer.ServerIsReady() )
  return _JsonResponse( True )


@app.post( '/user_options' )
def SetUserOptions():
  _logger.info( 'Received user options POST request' )
  UpdateUserOptions( request.json )


@app.post( '/semantic_completion_available' )
def FiletypeCompletionAvailable():
  _logger.info( 'Received filetype completion available request' )
  return _JsonResponse( _server_state.FiletypeCompletionAvailable(
      RequestWrap( request.json )[ 'filetypes' ] ) )


@app.post( '/defined_subcommands' )
def DefinedSubcommands():
  _logger.info( 'Received defined subcommands request' )
  completer = _GetCompleterForRequestData( RequestWrap( request.json ) )

  return _JsonResponse( completer.DefinedSubcommands() )


@app.post( '/detailed_diagnostic' )
def GetDetailedDiagnostic():
  _logger.info( 'Received detailed diagnostic request' )
  request_data = RequestWrap( request.json )
  completer = _GetCompleterForRequestData( request_data )

  return _JsonResponse( completer.GetDetailedDiagnostic( request_data ) )


@app.post( '/load_extra_conf_file' )
def LoadExtraConfFile():
  _logger.info( 'Received extra conf load request' )
  request_data = RequestWrap( request.json, validate = False )
  extra_conf_store.Load( request_data[ 'filepath' ], force = True )


@app.post( '/ignore_extra_conf_file' )
def IgnoreExtraConfFile():
  _logger.info( 'Received extra conf ignore request' )
  request_data = RequestWrap( request.json, validate = False )
  extra_conf_store.Disable( request_data[ 'filepath' ] )


@app.post( '/debug_info' )
def DebugInfo():
  _logger.info( 'Received debug info request' )

  output = []
  has_clang_support = ycm_core.HasClangSupport()
  output.append( 'Server has Clang support compiled in: {0}'.format(
    has_clang_support ) )

  if has_clang_support:
    output.append( 'Clang version: ' + ycm_core.ClangVersion() )

  request_data = RequestWrap( request.json )
  try:
    output.append(
        _GetCompleterForRequestData( request_data ).DebugInfo( request_data) )
  except:
    pass
  return _JsonResponse( '\n'.join( output ) )


# The type of the param is Bottle.HTTPError
@app.error( httplib.INTERNAL_SERVER_ERROR )
def ErrorHandler( httperror ):
  body = _JsonResponse( BuildExceptionResponse( httperror.exception,
                                                httperror.traceback ) )
  hmac_plugin.SetHmacHeader( body, _hmac_secret )
  return body


def _JsonResponse( data ):
  response.set_header( 'Content-Type', 'application/json' )
  return json.dumps( data, default = _UniversalSerialize )


def _UniversalSerialize( obj ):
  serialized = obj.__dict__.copy()
  serialized[ 'TYPE' ] = type( obj ).__name__
  return serialized


def _GetCompleterForRequestData( request_data ):
  completer_target = request_data.get( 'completer_target', None )

  if completer_target == 'identifier':
    return _server_state.GetGeneralCompleter().GetIdentifierCompleter()
  elif completer_target == 'filetype_default' or not completer_target:
    return _server_state.GetFiletypeCompleter( request_data[ 'filetypes' ] )
  else:
    return _server_state.GetFiletypeCompleter( [ completer_target ] )


@atexit.register
def ServerShutdown():
  _logger.info( 'Server shutting down' )
  if _server_state:
    _server_state.Shutdown()
    extra_conf_store.Shutdown()


def SetHmacSecret( hmac_secret ):
  global _hmac_secret
  _hmac_secret = hmac_secret


def UpdateUserOptions( options ):
  global _server_state

  if not options:
    return

  # This should never be passed in, but let's try to remove it just in case.
  options.pop( 'hmac_secret', None )
  user_options_store.SetAll( options )
  _server_state = server_state.ServerState( options )


def SetServerStateToDefaults():
  global _server_state, _logger
  _logger = logging.getLogger( __name__ )
  user_options_store.LoadDefaults()
  _server_state = server_state.ServerState( user_options_store.GetAll() )
  extra_conf_store.Reset()
