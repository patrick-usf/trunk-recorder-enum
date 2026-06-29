/* -*- c++ -*- */

/*
 * construct P25 frames out of raw dibits
 * Copyright 2010, KA1RBI
 * Copyright 2020, Graham J. Norbury
 *
 * usage: after constructing, call rx_sym once per received dibit.
 * frame fields are available for inspection when true is returned
 */

#ifndef INCLUDED_P25_FRAMER_H
#define INCLUDED_P25_FRAMER_H

#include "log_ts.h"

class p25_framer
{
    private:
        typedef std::vector<bool> bit_vector;
        // internal functions
        bool nid_codeword(uint64_t acc);
        // internal instance variables and state
        int nid_syms;
        uint32_t next_bit;
        uint64_t nid_accum;

        uint32_t frame_size_limit;
        int d_debug;
        int d_msgq_id;
        uint32_t d_expected_nac;
        int d_unexpected_nac;
        log_ts& logts;

    public:
        p25_framer(log_ts& logger, int debug = 0, int msgq_id = 0);
        ~p25_framer ();	// destructor
        void set_nac(uint32_t nac) { d_expected_nac = nac; }
        void set_debug(int debug) { d_debug = debug; }
        bool rx_sym(uint8_t dibit) ;
        uint32_t load_nid(const uint8_t *syms, int nsyms, const uint64_t fs);
        bool load_body(const uint8_t * syms, int nsyms);
        uint32_t get_frame_size_limit() const { return frame_size_limit; }

        uint32_t symbols_received;

        // info from received frame
        uint64_t nid_word;	// received NID word
        uint32_t nac;		// extracted NAC
        uint32_t duid;		// extracted DUID
        uint8_t  parity;	// extracted DUID parity
        bit_vector frame_body;	// all bits in frame
        uint32_t frame_size;	// number of bits in frame_body
        uint32_t bch_errors;	// number of errors detected in bch
        uint64_t raw_fs  = 0;   // received 48-bit FS word at sync detection
        uint64_t raw_nid = 0;   // received 64-bit NID before BCH correction
        uint8_t frame_end_reason = 0; // 1=size_limit, 2=new_sync, 3=load_body
};

#endif /* INCLUDED_P25_FRAMER_H */
