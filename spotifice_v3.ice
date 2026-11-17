[["underscore"]]
module Spotifice {
    class TrackInfo {
        string id;
        string title;
    };

    sequence<byte> AudioChunk;
    sequence<TrackInfo> TrackInfoSeq;

    exception Error {
        optional(1) string item;
        string reason;
    };

    exception IOError extends Error{};
    exception BadIdentity extends Error{};
    exception BadReference extends Error{};
    exception PlayerError extends Error{};
    exception StreamError extends Error{};
    exception TrackError extends Error{};
    exception AuthError extends Error{};  // new in version 3

    // new in version 3
    struct UserInfo {
        string username;
        string fullname;
        string email;
        long created_at;
    }

    // new in version 3
    interface Session {
        idempotent UserInfo get_user_info();
        idempotent void close();
    }

    interface MusicLibrary {
        idempotent TrackInfoSeq get_all_tracks() throws IOError;
        idempotent TrackInfo get_track_info(string track_id) throws IOError, TrackError;
    };

    // new in version 3
    ["deprecate:StreamManager is deprecated, use authenticate()"]
    interface StreamManager {};

    // new in version 3
    interface SecureStreamManager extends Session {
        idempotent void open_stream(string track_id) throws IOError, TrackError;
        idempotent void close_stream();
        AudioChunk get_chunk(int chunk_size) throws IOError, StreamError;
    };

    // new in version 3
    // foreward declaration
    interface MediaRender;
    
    // new in version 3
    interface AuthManager {
        SecureStreamManager* authenticate(MediaRender* media_render, string username, string password)
            throws AuthError, BadReference;
    };

    interface MediaProvider extends MusicLibrary, AuthManager {};

    interface RenderConnectivity {
        idempotent void bind_media_provider(MediaProvider* provider, SecureStreamManager* secure_stream_manager)
            throws BadReference;
        idempotent void unbind_media_provider();
    };

    interface ContentManager {
        idempotent void load_track(string track_id)
            throws BadReference, IOError, PlayerError, StreamError, TrackError;
        idempotent TrackInfo get_current_track();
    };

    interface PlaybackController {
        void play() throws BadReference, IOError, PlayerError, StreamError, TrackError;
        idempotent void stop() throws PlayerError;
    };

    interface MediaRender extends RenderConnectivity, ContentManager, PlaybackController {};
};
