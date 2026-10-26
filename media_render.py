#!/usr/bin/env python3

import logging
import signal
import sys
import threading
import uuid
from contextlib import contextmanager
from time import sleep

import Ice
from Ice import identityToString as id2str

from gst_player import GstPlayer

#Importante cambiar el slice con el que vayamos a trabajar
Ice.loadSlice(f'-I{Ice.getSliceDir()} spotifice_v1.ice')
import Spotifice  # type: ignore # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MediaRender")


class MediaRenderI(Spotifice.MediaRender):
    def __init__(self, player):
        self.player = player
        self.provider = None
        #Nos dicen que al arrancar, estará en STOPPED y sin pista cargada
        #Necesitamos meter una variable booleana para saber si la repetición está activada o no
        #Y una lista para el control de canciones
        self.state = Spotifice.PlaybackState.STOPPED
        self.current_track = None
        self.is_repeating = False
        self.history = []

    def ensure_player_stopped(self):
        if self.player.is_playing():
            raise Spotifice.PlayerError(reason="Already playing")

    def ensure_provider_bound(self):
        if not self.provider:
            raise Spotifice.BadReference(reason="No MediaProvider bound")

    # --- RenderConnectivity ---

    def bind_media_provider(self, media_provider, current=None):
        try:
            proxy = media_provider.ice_timeout(500)
            proxy.ice_ping()
        except Ice.ConnectionRefusedException as e:
            raise Spotifice.BadReference(reason=f"MediaProvider not reachable: {e}")

        self.provider = media_provider
        logger.info(f"Bound to MediaProvider '{id2str(media_provider.ice_getIdentity())}'")
        #En caso de que todo vaya bien, eliminamos el historial
        self.history.clear()

    def unbind_media_provider(self, current=None):
        try:
            self.stop(current)
        except Spotifice.PlayerError as e:
            logger.error(f"Unbinding despite stop failure: {e.reason}")

        self.provider = None
        logger.info("Unbound MediaProvider")
        #Aquí, lo mismo, eliminamos el historial cuando acabamos 
        self.history.clear()

    # --- ContentManager ---

    def load_track(self, track_id, current=None):
        self.ensure_provider_bound()

        #Cada pista cargada con load_track() se añade al histórico, aunque ya aparezca en él. La única
        #excepción es volver a cargar la pista actual, en ese caso el histórico no cambia.
        #Es decir, tenemos que asegurarnos de que no metemos la pista en el historial si es la misma que ya está sonando
        #Si la lista está vacía o el ultimo elemento de la lista es diferente a la canción que queremos meter.
        
        try:
            with self.keep_playing_state(current):
                self.current_track = self.provider.get_track_info(track_id)
            if not self.history or self.history[-1] != track_id:
                #Si no son iguales, la metemos
                self.history.append(track_id)

            logger.info(f"Current track set to: {self.current_track.title}")

        except Spotifice.TrackError as e:
            logger.error(f"Error setting track: {e.reason}")
            raise

    def get_current_track(self, current=None):
        return self.current_track

    # --- PlaybackController ---

    @contextmanager
    def keep_playing_state(self, current):
        #Antes de detener tenemos que guardar el estado, para recuperarlo más adelante
        previous_state = self.state
        
        #Comprobamos que esté o playing o paused
        playing = self.player.is_playing()
        if playing:
            #Y si lo está, lo paramos
            self.stop(current)
        try:
            yield
        finally:
            #Recuperamos el estado anterior, y actuamos en consecuencia
            if previous_state == Spotifice.PlaybackState.STOPPED:
                pass
            if previous_state == Spotifice.PlaybackState.PLAYING:
                self.play(current)
            if previous_state == Spotifice.PlaybackState.PAUSED:
                self.state = Spotifice.PlaybackState.PAUSED  

    def play(self, current=None):
        assert current, "remote invocation required"

        self.ensure_provider_bound()

        if not self.current_track:
            raise Spotifice.TrackError(reason="No track loaded")

        if self.state == Spotifice.PlaybackState.PAUSED:
            if self.player.is_playing():
                check = self.player.resume()
                if check:
                    self.state = Spotifice.PlaybackState.PLAYING
                else:
                    raise Spotifice.PlayerError(reason = "Cannot resume")
                return
        
        self.ensure_player_stopped()

        try:
            self.provider.open_stream(current.id, self.current_track.id)
        except Spotifice.BadIdentity as e:
            logger.error(f"Error starting stream: {e.reason}")
            raise Spotifice.StreamError(reason="Strean setup failed")

        #Creamos una función auxiliar, para pasarle la info de la llamada remota(current) el cual contiene la canción que tiene que reptir
        def al_terminar():
            self.on_track_exhausted(current)

        self.player.play(self.chunk_reader(current.id), al_terminar)
        if not self.player.confirm_play_starts():
            raise Spotifice.PlayerError(reason="Failed to confirm playback")
        
        self.state = Spotifice.PlaybackState.PLAYING

    def chunk_reader(self, render_id):
        provider = self.provider

        def read_chunk(chunk_size):
            try:
                return provider.get_chunk(render_id, chunk_size)
            except Spotifice.IOError as e:
                logger.error(f"Playback of track '{e.item}' aborted: {e.reason}")
            except Ice.Exception as e:
                logger.critical(e)

        return read_chunk

    def stop(self, current=None):
        if self.provider and current:
            self.provider.close_stream(current.id)

        if not self.player.stop():
            raise Spotifice.PlayerError(reason="Failed to confirm stop")

        #Importante actualizar el estado de la variable cuando lo paremos, igual en play y pause
        self.state = Spotifice.PlaybackState.STOPPED
        logger.info("Stopped")

    def on_track_exhausted(self, current=None):
        if self.is_repeating:
            #Ahora si lo tenemos porque en el play, hemos guardado en alterminar la info de la llamada remota
            self.play(current)
        else: 
            self.state = Spotifice.PlaybackState.STOPPED
            
#---------------------------------------------
    def get_status(self, current=None):
        #Solo tenemos que devolver un objeto PlaybackStatus, ya que así está definido en la interfaz de ice
        return Spotifice.PlaybackStatus(self.state, self.current_track, self.is_repeating)

    def pause(self, current = None):
        #STOPPED
        if self.state == Spotifice.PlaybackState.STOPPED:
            raise Spotifice.PlayerError(reason = "Cannot pause when stopped")
        
        #PLAYING
        if self.state == Spotifice.PlaybackState.PLAYING:
            pause_check = self.player.pause()
            if pause_check:
                self.state = Spotifice.PlaybackState.PAUSED
            else:
                raise Spotifice.PlayerError(reason = "Cannot pause")
        
        #PAUSED
        if self.state == Spotifice.PlaybackState.PAUSED:
            #No haríamos nada, porque es idempotente
            self.state = Spotifice.PlaybackState.PAUSED
            
    def previous(self, current=None):
        #Comprobamos que podemos volver atrás
        if len(self.history) > 1:
            #Antes de borrar, vamos a hacer un "backup" de la canción
            backup_track = self.history.pop()
            try:
                self.load_track(self.history[-1], current=current)
            except Spotifice.Error:   
                #En el caso de error, metemos la canción de respaldo de nuevo
                self.history.append(backup_track)
                #Y lanzamos el error
                raise
        else:
            pass
            
    #Importante fijarse en el .ice para darnos cuenta de que necesitamos pasarle enabled    
    def set_repeat(self, enabled, current=None):    
        if enabled == True:
            self.is_repeating = True
        elif enabled == False:
            self.is_repeating = False
            
#---------------------------------------------
def shutdown_on_interrupt(ic):
    def shutdown(*_):
        ic.shutdown()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT,  shutdown)
        signal.signal(signal.SIGTERM, shutdown)


def main(ic, player):
    shutdown_on_interrupt(ic)

    servant = MediaRenderI(player)

    properties = ic.getProperties()
    identity = properties.getPropertyWithDefault(
        'Spotifice.MediaRender.Identity',  str(uuid.uuid1()))

    adapter = ic.createObjectAdapter('Spotifice.MediaRenderAdapter')
    proxy = adapter.add(servant, ic.stringToIdentity(identity))
    logger.info(f"MediaRender: {proxy}")

    adapter.activate()
    while not ic.isShutdown():
        sleep(0.5)

    logger.info("Server shutdown.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: media_render.py --Ice.Config=<config-file>")

    with Ice.initialize(sys.argv) as communicator, GstPlayer() as player:
        main(communicator, player)
