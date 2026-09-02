#ifndef PY000_ZMQ_MQH
#define PY000_ZMQ_MQH
#define PY000_ZMQ_PUB       1
#define PY000_ZMQ_REP       4
#define PY000_ZMQ_DONTWAIT  1
#define PY000_ZMQ_SNDMORE   2
#define PY000_ZMQ_RCVHWM    24
#define PY000_ZMQ_SNDHWM    23
#define PY000_ZMQ_LINGER    17
#define PY000_ZMQ_RCVMORE   13
#define PY000_ZMQ_SNDTIMEO  28
#define PY000_ZMQ_MAX_WIRE  65536
#define PY000_ZMQ_MAX_MULTIPART_FRAMES 16
#define PY000_ZMQ_EAGAIN    11
#define PY000_ZMQ_RECV_FATAL -1
#define PY000_ZMQ_RECV_NONE   0
#define PY000_ZMQ_RECV_MESSAGE 1
#import "libzmq.dll"
long zmq_ctx_new();
int  zmq_ctx_term(long context);
long zmq_socket(long context, int socket_type);
int  zmq_close(long socket);
int  zmq_bind(long socket, uchar &endpoint[]);
int  zmq_setsockopt(long socket, int option, uchar &value[], long value_length);
int  zmq_getsockopt(long socket, int option, uchar &value[], long &value_length);
int  zmq_send(long socket, uchar &data[], long length, int flags);
int  zmq_recv(long socket, uchar &buffer[], long maximum_length, int flags);
int  zmq_errno();
#import
long g_py000_zmq_context = 0;
long g_py000_zmq_pub = 0;
long g_py000_zmq_rep = 0;
void Py000ZmqIntBytes(const int value, uchar &bytes[])
{
   ArrayResize(bytes, 4);
   bytes[0] = (uchar)(value & 0xff);
   bytes[1] = (uchar)((value >> 8) & 0xff);
   bytes[2] = (uchar)((value >> 16) & 0xff);
   bytes[3] = (uchar)((value >> 24) & 0xff);
}
bool Py000ZmqSetInt(const long socket, const int option, const int value)
{
   uchar bytes[];
   Py000ZmqIntBytes(value, bytes);
   return zmq_setsockopt(socket, option, bytes, 4) == 0;
}
bool Py000ZmqSetIntChecked(
   const long socket,
   const int option,
   const int value,
   const string label
)
{
   if(Py000ZmqSetInt(socket, option, value))
      return true;
   PrintFormat("PY000 ZMQ setsockopt failed option=%s errno=%d", label, zmq_errno());
   return false;
}
bool Py000ZmqGetInt(
   const long socket,
   const int option,
   int &value,
   string &failure_reason,
   int &failure_errno,
   bool &failure_has_errno
)
{
   failure_reason = "";
   failure_errno = 0;
   failure_has_errno = false;
   uchar bytes[];
   ArrayResize(bytes, 4);
   long length = 4;
   if(zmq_getsockopt(socket, option, bytes, length) != 0)
   {
      failure_reason = "zmq_getsockopt failed";
      failure_errno = zmq_errno();
      failure_has_errno = true;
      return false;
   }
   if(length != 4)
   {
      failure_reason = "zmq_getsockopt returned an invalid integer length";
      return false;
   }
   value = (int)bytes[0] | ((int)bytes[1] << 8)
      | ((int)bytes[2] << 16) | ((int)bytes[3] << 24);
   return true;
}
bool Py000ZmqBind(const long socket, const string endpoint)
{
   uchar encoded[];
   int count = StringToCharArray(endpoint, encoded, 0, WHOLE_ARRAY, CP_UTF8);
   if(count <= 1)
      return false;
   return zmq_bind(socket, encoded) == 0;
}
bool Py000ZmqBindChecked(
   const long socket,
   const string endpoint,
   const string label
)
{
   if(Py000ZmqBind(socket, endpoint))
      return true;
   PrintFormat(
      "PY000 ZMQ bind failed socket=%s endpoint=%s errno=%d",
      label,
      endpoint,
      zmq_errno()
   );
   return false;
}
int Py000ZmqSendUtf8(const long socket, const string value, const int flags)
{
   uchar encoded[];
   int count = StringToCharArray(value, encoded, 0, WHOLE_ARRAY, CP_UTF8);
   if(count <= 0)
      return -1;
   return zmq_send(socket, encoded, count - 1, flags);
}
void Py000ZmqShutdown()
{
   if(g_py000_zmq_pub != 0)
   {
      Py000ZmqSetInt(g_py000_zmq_pub, PY000_ZMQ_LINGER, 0);
      zmq_close(g_py000_zmq_pub);
      g_py000_zmq_pub = 0;
   }
   if(g_py000_zmq_rep != 0)
   {
      Py000ZmqSetInt(g_py000_zmq_rep, PY000_ZMQ_LINGER, 0);
      zmq_close(g_py000_zmq_rep);
      g_py000_zmq_rep = 0;
   }
   if(g_py000_zmq_context != 0)
   {
      zmq_ctx_term(g_py000_zmq_context);
      g_py000_zmq_context = 0;
   }
}
bool Py000ZmqInit(const string pub_bind, const string rep_bind)
{
   if(g_py000_zmq_context != 0)
      return false;
   g_py000_zmq_context = zmq_ctx_new();
   if(g_py000_zmq_context == 0)
      return false;
   g_py000_zmq_pub = zmq_socket(g_py000_zmq_context, PY000_ZMQ_PUB);
   g_py000_zmq_rep = zmq_socket(g_py000_zmq_context, PY000_ZMQ_REP);
   if(g_py000_zmq_pub == 0 || g_py000_zmq_rep == 0)
   {
      Py000ZmqShutdown();
      return false;
   }
   bool configured =
      Py000ZmqSetIntChecked(
         g_py000_zmq_pub, PY000_ZMQ_LINGER, 0, "PUB LINGER"
      )
      && Py000ZmqSetIntChecked(
         g_py000_zmq_rep, PY000_ZMQ_LINGER, 0, "REP LINGER"
      )
      && Py000ZmqSetIntChecked(
         g_py000_zmq_rep, PY000_ZMQ_SNDTIMEO, 0, "REP SNDTIMEO"
      )
      && Py000ZmqSetIntChecked(
         g_py000_zmq_pub, PY000_ZMQ_SNDHWM, 1000, "PUB SNDHWM"
      )
      && Py000ZmqSetIntChecked(
         g_py000_zmq_pub, PY000_ZMQ_RCVHWM, 1000, "PUB RCVHWM"
      )
      && Py000ZmqSetIntChecked(
         g_py000_zmq_rep, PY000_ZMQ_SNDHWM, 100, "REP SNDHWM"
      )
      && Py000ZmqSetIntChecked(
         g_py000_zmq_rep, PY000_ZMQ_RCVHWM, 100, "REP RCVHWM"
      );
   if(!configured
      || !Py000ZmqBindChecked(g_py000_zmq_pub, pub_bind, "PUB")
      || !Py000ZmqBindChecked(g_py000_zmq_rep, rep_bind, "REP"))
   {
      Py000ZmqShutdown();
      return false;
   }
   return true;
}
int Py000ZmqRepTryRecv(
   string &request,
   string &malformed_reason,
   string &fatal_reason,
   int &fatal_errno,
   bool &fatal_has_errno
)
{
   request = "";
   malformed_reason = "";
   fatal_reason = "";
   fatal_errno = 0;
   fatal_has_errno = false;
   if(g_py000_zmq_rep == 0)
   {
      fatal_reason = "REP socket is unavailable";
      return PY000_ZMQ_RECV_FATAL;
   }
   uchar buffer[];
   ArrayResize(buffer, PY000_ZMQ_MAX_WIRE + 1);
   int received = zmq_recv(
      g_py000_zmq_rep,
      buffer,
      PY000_ZMQ_MAX_WIRE + 1,
      PY000_ZMQ_DONTWAIT
   );
   if(received < 0)
   {
      int receive_errno = zmq_errno();
      if(receive_errno == PY000_ZMQ_EAGAIN)
         return PY000_ZMQ_RECV_NONE;
      fatal_reason = "initial zmq_recv failed";
      fatal_errno = receive_errno;
      fatal_has_errno = true;
      return PY000_ZMQ_RECV_FATAL;
   }
   int more = 0;
   if(!Py000ZmqGetInt(
         g_py000_zmq_rep,
         PY000_ZMQ_RCVMORE,
         more,
         fatal_reason,
         fatal_errno,
         fatal_has_errno
      ))
      return PY000_ZMQ_RECV_FATAL;
   bool multipart = more != 0;
   int frame_count = 1;
   long total_received = received;
   while(more != 0)
   {
      if(frame_count >= PY000_ZMQ_MAX_MULTIPART_FRAMES)
      {
         fatal_reason = "multipart frame budget exceeded";
         return PY000_ZMQ_RECV_FATAL;
      }
      if(total_received > PY000_ZMQ_MAX_WIRE)
      {
         fatal_reason = "multipart byte budget exceeded";
         return PY000_ZMQ_RECV_FATAL;
      }
      int drained = zmq_recv(
         g_py000_zmq_rep,
         buffer,
         PY000_ZMQ_MAX_WIRE + 1,
         PY000_ZMQ_DONTWAIT
      );
      if(drained < 0)
      {
         fatal_reason = "multipart zmq_recv failed";
         fatal_errno = zmq_errno();
         fatal_has_errno = true;
         return PY000_ZMQ_RECV_FATAL;
      }
      frame_count++;
      total_received += drained;
      if(total_received > PY000_ZMQ_MAX_WIRE)
      {
         fatal_reason = "multipart byte budget exceeded";
         return PY000_ZMQ_RECV_FATAL;
      }
      if(!Py000ZmqGetInt(
            g_py000_zmq_rep,
            PY000_ZMQ_RCVMORE,
            more,
            fatal_reason,
            fatal_errno,
            fatal_has_errno
         ))
         return PY000_ZMQ_RECV_FATAL;
   }
   if(multipart)
   {
      malformed_reason = "multipart requests are not supported";
      return PY000_ZMQ_RECV_MESSAGE;
   }
   if(received == 0)
   {
      malformed_reason = "empty request frame";
      return PY000_ZMQ_RECV_MESSAGE;
   }
   if(received > PY000_ZMQ_MAX_WIRE)
   {
      malformed_reason = "wire payload exceeds 64 KiB";
      return PY000_ZMQ_RECV_MESSAGE;
   }
   request = CharArrayToString(buffer, 0, received, CP_UTF8);
   uchar roundtrip[];
   int count = StringToCharArray(request, roundtrip, 0, WHOLE_ARRAY, CP_UTF8);
   if(count != received + 1)
   {
      malformed_reason = "wire payload is not UTF-8";
      request = "";
      return PY000_ZMQ_RECV_MESSAGE;
   }
   for(int index = 0; index < received; index++)
   {
      if(roundtrip[index] != buffer[index])
      {
         malformed_reason = "wire payload is not UTF-8";
         request = "";
         return PY000_ZMQ_RECV_MESSAGE;
      }
   }
   return PY000_ZMQ_RECV_MESSAGE;
}
bool Py000ZmqRepSend(
   const string response,
   string &fatal_reason,
   int &fatal_errno,
   bool &fatal_has_errno
)
{
   fatal_reason = "";
   fatal_errno = 0;
   fatal_has_errno = false;
   if(g_py000_zmq_rep == 0)
   {
      fatal_reason = "REP socket is unavailable";
      return false;
   }
   uchar encoded[];
   int count = StringToCharArray(response, encoded, 0, WHOLE_ARRAY, CP_UTF8);
   if(count <= 1)
   {
      fatal_reason = "REP response is empty or cannot be encoded";
      return false;
   }
   if(count - 1 > PY000_ZMQ_MAX_WIRE)
   {
      fatal_reason = "REP response exceeds the wire budget";
      return false;
   }
   int sent = zmq_send(
      g_py000_zmq_rep,
      encoded,
      count - 1,
      PY000_ZMQ_DONTWAIT
   );
   if(sent < 0)
   {
      fatal_reason = "REP zmq_send failed";
      fatal_errno = zmq_errno();
      fatal_has_errno = true;
      return false;
   }
   if(sent != count - 1)
   {
      fatal_reason = "REP zmq_send was short";
      return false;
   }
   return true;
}
bool Py000ZmqPubSend(const string topic, const string payload)
{
   if(g_py000_zmq_pub == 0)
      return false;
   if(Py000ZmqSendUtf8(
         g_py000_zmq_pub,
         topic,
         PY000_ZMQ_SNDMORE | PY000_ZMQ_DONTWAIT
      ) <= 0)
      return false;
   return Py000ZmqSendUtf8(g_py000_zmq_pub, payload, PY000_ZMQ_DONTWAIT) > 0;
}
#endif
