#ifndef PARSE_H
#define PARSE_H
#include <cstdint>
#include <iostream>
#include <vector>
#include <string>

enum FrameDirection {
  DIR_OSP     = 0, // Outbound Subscriber PDU — tower → radio (downlink/control channel)
  DIR_ISP     = 1, // Inbound Subscriber PDU  — radio → tower (uplink)
  DIR_UNKNOWN = 2
};

enum MessageType {
  GRANT = 0,
  STATUS = 1,
  UPDATE = 2,
  CONTROL_CHANNEL = 3,
  REGISTRATION = 4,
  DEREGISTRATION = 5,
  AFFILIATION = 6,
  SYSID = 7,
  ACKNOWLEDGE = 8,
  LOCATION = 9,
  PATCH_ADD = 10,
  PATCH_DELETE = 11,
  DATA_GRANT = 12,
  UU_ANS_REQ = 13,
  UU_V_GRANT = 14,
  UU_V_UPDATE = 15,
  INVALID_CC_MESSAGE = 16,
  TDULC = 17,
  UNKNOWN = 99
};

struct PatchData {
  unsigned long sg;
  unsigned long ga1;
  unsigned long ga2;
  unsigned long ga3;
};

struct TrunkMessage {
  MessageType message_type = UNKNOWN;
  std::string meta;
  double freq = 0.0;
  long talkgroup = 0;
  bool encrypted = false;
  bool emergency = false;
  bool duplex = false;
  bool mode = false;
  int priority = 0;
  int tdma_slot = 0;
  bool phase2_tdma = false;
  long source = -1;
  int sys_num = 0;
  unsigned long sys_id = 0;
  int sys_rfss = 0;
  int sys_site_id = 0;
  unsigned long nac = 0;
  unsigned long wacn = 0;
  PatchData patch_data = {};
  unsigned long opcode = 255;
  unsigned long mfid = 0;        // Manufacturer ID byte (0x00=standard, 0x90=Motorola, 0xA4=M/A-COM)
  FrameDirection direction = DIR_UNKNOWN;  // DIR_OSP=downlink, DIR_ISP=uplink
  std::string raw_frame;         // hex-encoded raw bytes for unknown/undecoded frames
  uint8_t duid = 0;              // P25 DUID from NID (0x07=TSBK, 0x0c=PDU, 0x0f=TDULC, 0x05=LDU1, etc.)
  std::string fec;               // FEC error statistics, e.g. "HMG(d=0,c=0,r=0)|RS8(d=0,c=0,r=0)"
  double recv_freq = 0.0;        // always the tuned channel frequency, never overridden by payload
};

class TrunkParser {
  std::vector<TrunkMessage> parse_message(std::string s);
};
#endif
