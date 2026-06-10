/*
 * p25-data-monitor — standalone P25 data channel passive monitor
 *
 * Receives wideband IQ from trunk-recorder's ZMQ publisher, tunes to a
 * P25 Phase-1 data channel, decodes FSK4, and logs all parsed frames to
 * a dedicated TSV file via P25FrameLogger (same format as trunk-recorder).
 *
 * Usage:
 *   p25-data-monitor --zmq tcp://127.0.0.1:5556 \
 *                   --center 856500000           \
 *                   --rate   10800000            \
 *                   --freq   851538000           \
 *                   --log    /logs/p25_data.tsv  \
 *                   [--sys-name HowardCounty]
 */

#include <atomic>
#include <getopt.h>
#include <iostream>
#include <signal.h>
#include <string>
#include <unistd.h>

#include <gnuradio/msg_queue.h>
#include <gnuradio/message.h>
#include <gnuradio/top_block.h>
#include <gnuradio/zeromq/sub_source.h>

#include "global_structs.h"
#include "gr_blocks/xlat_channelizer.h"
#include "recorders/p25_recorder_decode.h"
#include "recorders/p25_recorder_fsk4_demod.h"
#include "recorders/recorder.h"
#include "systems/p25_frame_logger.h"
#include "systems/p25_parser.h"
#include "systems/system.h"

static std::atomic<bool> running{true};

static void sig_handler(int) { running = false; }

// Minimal Recorder stub: p25_recorder_decode only calls
// get_enable_audio_streaming() during initialization.
class DataMonitorRecorder : public Recorder {
public:
  DataMonitorRecorder() : Recorder(P25) {
    recording_count          = 0;
    recording_duration       = 0.0;
    d_enable_audio_streaming = false;
    conventional             = false;
    selector_port            = 0;
    rec_num                  = 0;
  }
};

static void print_usage(const char *prog) {
  std::cerr << "Usage: " << prog << "\n"
            << "  --zmq    <address>  ZMQ SUB address (e.g. tcp://127.0.0.1:5556)\n"
            << "  --center <hz>       SDR center frequency in Hz\n"
            << "  --rate   <sps>      SDR sample rate in samples/s\n"
            << "  --freq   <hz>       Data channel frequency in Hz\n"
            << "  --log    <path>     Output TSV log file path\n"
            << " [--sys-name <name>]  System short name for log (default: data-monitor)\n";
}

int main(int argc, char **argv) {
  signal(SIGINT,  sig_handler);
  signal(SIGTERM, sig_handler);

  std::string zmq_addr;
  double      sdr_center = 0.0;
  double      sdr_rate   = 0.0;
  double      data_freq  = 0.0;
  std::string log_path;
  std::string sys_name = "data-monitor";

  static const struct option long_opts[] = {
    { "zmq",      required_argument, 0, 'z' },
    { "center",   required_argument, 0, 'c' },
    { "rate",     required_argument, 0, 'r' },
    { "freq",     required_argument, 0, 'f' },
    { "log",      required_argument, 0, 'l' },
    { "sys-name", required_argument, 0, 'n' },
    { 0, 0, 0, 0 }
  };

  int opt, idx;
  while ((opt = getopt_long(argc, argv, "z:c:r:f:l:n:", long_opts, &idx)) != -1) {
    switch (opt) {
      case 'z': zmq_addr   = optarg;           break;
      case 'c': sdr_center = std::stod(optarg); break;
      case 'r': sdr_rate   = std::stod(optarg); break;
      case 'f': data_freq  = std::stod(optarg); break;
      case 'l': log_path   = optarg;           break;
      case 'n': sys_name   = optarg;           break;
      default:
        print_usage(argv[0]);
        return 1;
    }
  }

  if (zmq_addr.empty() || sdr_center == 0.0 || sdr_rate == 0.0 ||
      data_freq == 0.0 || log_path.empty()) {
    print_usage(argv[0]);
    return 1;
  }

  // Minimal System — needed by P25Parser (freq table, sys_num) and
  // P25FrameLogger (short_name). sys_num=0 means no custom freq table;
  // channel-to-freq lookups inside data PDUs will return 0 (acceptable).
  System *sys = System::make(0);
  sys->set_short_name(sys_name);
  sys->set_system_type("p25");

  // Open dedicated data log; parse_message() calls log_messages() internally.
  P25FrameLogger::instance().open(log_path);
  if (!P25FrameLogger::instance().is_open()) {
    std::cerr << "[p25-data-monitor] ERROR: cannot open log file: " << log_path << "\n";
    delete sys;
    return 1;
  }

  // ── Build GR flowgraph ──────────────────────────────────────────────────
  gr::top_block_sptr tb = gr::make_top_block("p25-data-monitor");

  // ZMQ subscriber — connects to trunk-recorder's wideband IQ publisher
  auto zmq_src = gr::zeromq::sub_source::make(
      sizeof(gr_complex), 1,
      const_cast<char *>(zmq_addr.c_str()));

  // Channelizer: frequency-translate data_freq to baseband, decimate to
  // P25 channel rate (5 samp/sym × 4800 sym/s = 24 ksps out).
  // tune_offset(center - freq) → set_center_freq(freq - center) in GR.
  auto xlat = xlat_channelizer::make(
      sdr_rate,
      xlat_channelizer::phase1_samples_per_symbol,
      xlat_channelizer::phase1_symbol_rate,
      xlat_channelizer::channel_bandwidth,
      sdr_center,
      false);  // use_squelch=false — passive, always open
  xlat->tune_offset(sdr_center - data_freq);

  // FSK4 demodulator: gr_complex → float symbols
  auto fsk4 = make_p25_recorder_fsk4_demod();

  // Frame assembler chain (float → audio[dropped internally] + rx_queue frames)
  DataMonitorRecorder rec_stub;
  auto decode = make_p25_recorder_decode(&rec_stub, 0, false);

  tb->connect(zmq_src, 0, xlat,   0);
  tb->connect(xlat,    0, fsk4,   0);
  tb->connect(fsk4,    0, decode, 0);

  tb->start();

  std::cerr << "[p25-data-monitor] running"
            << "  zmq="    << zmq_addr
            << "  freq="   << data_freq
            << "  center=" << sdr_center
            << "  log="    << log_path << "\n";

  // ── Polling loop ────────────────────────────────────────────────────────
  P25Parser            parser;
  gr::msg_queue::sptr  rx_q = decode->get_rx_queue();

  while (running) {
    gr::message::sptr msg = rx_q->delete_head_nowait();
    if (msg) {
      if (msg->type() >= 0) {
        // parse_message() decodes the frame and calls P25FrameLogger internally.
        // fallback_freq fills freq_mhz for frames (TDULC, LCW, PDU) that don't
        // carry their own channel frequency.
        parser.parse_message(msg, sys, data_freq);
      }
    } else {
      usleep(5000);  // 5 ms — ~20× shorter than a P25 frame burst
    }
  }

  std::cerr << "[p25-data-monitor] shutting down\n";
  tb->stop();
  tb->wait();
  P25FrameLogger::instance().close();
  delete sys;
  return 0;
}
