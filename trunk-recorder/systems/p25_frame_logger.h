#pragma once
#include <fstream>
#include <mutex>
#include <string>
#include <vector>
#include "parser.h"
#include "system.h"

class P25FrameLogger {
public:
  static P25FrameLogger &instance();

  void open(const std::string &path);
  void close();
  bool is_open() const;

  // Log all TrunkMessages from a single decoded frame. frame_type mirrors the
  // GNURadio message type: 7=TSBK, 12=MBT/PDU, 18=Phase2 MAC PDU, 20=raw non-MBT PDU.
  void log_messages(const std::vector<TrunkMessage> &messages, System *system, int frame_type);

private:
  P25FrameLogger() = default;
  ~P25FrameLogger();
  P25FrameLogger(const P25FrameLogger &) = delete;
  P25FrameLogger &operator=(const P25FrameLogger &) = delete;

  void write_header();
  std::string format_record(const TrunkMessage &msg, System *system, int frame_type) const;
  std::string opcode_name(unsigned long opcode, unsigned long mfid, int frame_type) const;
  std::string decode_status(const TrunkMessage &msg) const;
  std::string ts_now() const;

  std::ofstream log_file_;
  mutable std::mutex mtx_;
};
