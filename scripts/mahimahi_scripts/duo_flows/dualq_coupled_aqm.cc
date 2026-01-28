#include <algorithm>
//#include <iostream>

#include "dualq_coupled_aqm.hh"
#include "timestamp.hh"
#include "exception.hh"
#include "ezio.hh"

#include "abstract_packet_queue.hh"


using namespace std;
using namespace PollerShortNames;

DualQCoupledAQM::DualQCoupledAQM( const string & args )
  : byte_limit_( get_arg( args, "bytes" ) ),
    packet_limit_( get_arg( args, "packets" ) ),
    //k_ ( get_arg( args, "k" ) ),
    l4s_queue_ ( L4SPacketQueue ( "" ) ),
    classic_queue_ ( CLASSICPacketQueue ( "" ) ),
    scheduler_type_ ( static_cast<SchedulerType> (get_arg( args, "sched" ))),
    target_ms_ ( get_arg( args, "target" ) ),
    max_rtt_ms_ ( get_arg( args, "max_rtt" ) ),
    alpha_ ( get_arg( args, "alpha" ) ),
    beta_ ( get_arg( args, "beta" ) ),
    t_update_ms_ ( get_arg( args, "tupdate" ) ),
    overload_drop_pkts_ ( 0 ),
    overflow_drop_pkts_ ( 0 ),
    not_ect_drop_pkts_ ( 0 ),
    pp_ ( 0 ),
    pp_l_ ( 0 ),
    p_l_ ( 0 ),
    p_cl_ ( 0 ),
    p_c_ ( 0 ),
    k_ ( 2 ),
    l4s_drop_on_overload_ ( true )
{
    start_ts_ns_ = timestamp();
    last_log_ts_ns_ = start_ts_ns_;

    if ( packet_limit_ == 0 and byte_limit_ == 0 ) {
        packet_limit_ = 10000; /* default value from Linux code. Represents 125 ms at 1 Gbps */
        byte_limit_ = packet_limit_ * MTU;
    }
    else if (packet_limit_ != 0) {
        // Prioritize packet_limit_ over byte_limit_
        byte_limit_ = packet_limit_ * MTU;

    }
    else if (byte_limit_ != 0) {
        packet_limit_ = byte_limit_ / MTU;
    }

    //if ( k_ == 0 ) k_ = 2;

    // For equivalence with the Linux kernel code
    // p_Cmax_ = min( 1/ pow( k_, 2 ) , 1.0 );
    // p_Lmax_ = 1.0;

    max_prob = 1.0;

    if ( target_ms_ == 0 ) target_ms_ = 15; 
    if ( max_rtt_ms_ == 0 ) max_rtt_ms_ = 100;
    if ( t_update_ms_ == 0 ) t_update_ms_ = 16; // RFC 9332: Tupdate = min(target, RTT_max/3)

    /* From RFC 9332:
        13:   alpha = 0.1 * Tupdate / RTT_max^2      % PI integral gain in Hz
        14:   beta = 0.3 / RTT_max                   % PI proportional gain in Hz */
    
    // Since the default time unit is ms, alpha and beta have to be in kHz
    if ( alpha_ == 0 ) alpha_ = 0.00016;
    if ( beta_ == 0 ) beta_ = 0.0032;
    

    if (scheduler_type_ == SchedulerType::WRR) {
        scheduler_ = std::unique_ptr<WRRScheduler>( new WRRScheduler(l4s_queue_, classic_queue_) );
    }

    l4s_qdelay_ms_ = 0;
    classic_qdelay_ms_ = 0;

    drop_l4s_pkts_ = 0;
    drop_classic_pkts_ = 0;
    last_dequeue_from_ = QueueType::NONE;
    
    /* initialize base timestamp value */
    //initial_timestamp_ns();

    /* Start the periodic process that updates probs*/
    
    set_periodic_update ();
    set_queue_log_timer_1ms();

    


    ////std::cout << "end of the ctor " << std::endl;
    ////std::cout << "packet_limit_= " << std::to_string(packet_limit_) << " byte_limit_= " << std::to_string(byte_limit_) << std::endl;
}

void DualQCoupledAQM::set_queue_log_timer_1ms( void )
{
    const timespec interval { 0, 16 * NS_PER_MS }; // 1 ms
    queue_log_timer_.set_time( interval, interval );

    poller_.add_action(
        Poller::Action(
            queue_log_timer_,
            Direction::In,
            [&]() {
                // consume timerfd
                queue_log_timer_.read();

                const uint64_t now_ns = timestamp();
                if (start_ts_ns_ == 0) {
                    start_ts_ns_ = now_ns;
                }

                const double t_ms = (now_ns - start_ts_ns_) / 1e6;

                std::cout
                    << "[QUEUE_STATS]"
                    << " t_ms=" << t_ms
                    << " q_pkts=" << size_packets()
                    << " q_bytes=" << size_bytes()
                    << " ecn_mark=" << mark_pkts_
                    << " drop_l4s=" << drop_l4s_pkts_
                    << " drop_classic=" << drop_classic_pkts_
                    << " drop_overload=" << overflow_drop_pkts_
                    << std::endl;

                return ResultType::Continue;
            }
        )
    );
}

void DualQCoupledAQM::enqueue( QueuedPacket && p )
{
    // 1 MTU of space is always allowed (assumed size of the arriving packet) 
    // to avoid bias against larger packets. Might end up causing 
    // underutilization of buffer space...
    // Use p.contents.size() instead of MTU to be more precise

    ////std::cout << "--In DualQCoupled AQM enqueue" << std::endl;

    // check if the periodic update function is due, return immediately if not.
    // //std::cout << "> Polling (start of enqueue)" << std::endl;
    poller_.poll( 0 );

    if ( size_bytes() + MTU > byte_limit_) {
        // //std::cout << "> Drop due to overflow!! " << std::endl;
        drop (DropReason::Overflow);
        return;
    }

    // Record the packet's timestamp to calculate the sojourn time. 
    // Here, I am using the existing p.arrival_time, similar to CoDel.

    // Packet classifier
    unsigned char ecn_bits = get_ecn_bits( p );

    ////std::cout << "> ECN bits: " << std::to_string(ecn_bits) << std::endl;

    if (( ecn_bits == IPTOS_ECN_ECT1 ) ||
        ( ecn_bits == IPTOS_ECN_CE )) {
            //std::cout << "> Calling L4S enqueue... " << std::endl;

        l4s_queue_.enqueue( std::move( p ) );

    } else {
        //std::cout << "> Calling Classic enqueue... " << std::endl;
        classic_queue_.enqueue( std::move( p ) );
    }
    
    // check if the periodic update function is due, return immediately if not.
    //std::cout << "> Polling (end of enqueue)" << std::endl;
    poller_.poll( 0 );
}

QueuedPacket DualQCoupledAQM::dequeue( void )
{
    //std::cout << "--In DualQCoupled AQM dequeue" << std::endl;

    QueueType dequeue_from;
    uint64_t l4s_qdelay_ms;
    uint64_t now;

    do {
        // check if the periodic update function is due, return immediately if not.
        //std::cout << "> Polling (start of dequeue iteration)" << std::endl;
        poller_.poll( 0 );

        QueuedPacket pkt("", 0);
        dequeue_from = scheduler_->select_queue();
        last_dequeue_from_ = dequeue_from;

        if ( dequeue_from == QueueType::L4S ) {
            //std::cout << "> Scheduler selects L4S..." << std::endl;
            pkt = l4s_queue_.dequeue();
            
            if ( not is_overloaded() ) {
                now = timestamp();

                l4s_qdelay_ms = l4s_queue_.qdelay_in_ms( now );
                pp_l_ = l4s_queue_.calculate_l4s_native_prob( l4s_qdelay_ms ); 

                p_l_ = max(pp_l_, p_cl_);

                if ( roll( p_l_) ) {
                    // if ( can_mark_or_drop() )
                    // {
                        mark( pkt );
                    // }
                }                      
            } else {
                if ( roll( p_c_) ) {
                    if ( can_mark_or_drop() ) 
                    {
                        drop(DropReason::Overload);
                        continue;
                    }
                } 

                // if ( roll( p_cl_ ) ) {
                    if ( can_mark_or_drop() )
                    {
                        mark( pkt );
                    }
                //} 
            }
            scheduler_update();
        } 
        else if ( dequeue_from == QueueType::Classic ) { 
            //std::cout << "> Scheduler selects Classic..." << std::endl;
            pkt = classic_queue_.dequeue();       
            
            if ( roll( p_c_) ) {
                if ( get_ecn_bits( pkt ) == IPTOS_ECN_NOT_ECT ||
                    is_overloaded() ) {
                        if ( can_mark_or_drop() )
                        {
                            //std::cout << " ---- DROPPING !! " << std::endl;
                            if (is_overloaded()) drop(DropReason::Overload);
                            else drop(DropReason::NotECT);
                            continue;
                        }
                }
                if ( can_mark_or_drop() )
                {
                    //std::cout << " ---- MARKING !! " << std::endl;
                    mark( pkt );
                }
            }
            scheduler_update();
        }

        // check if the periodic update function is due, return immediately if not.
        //std::cout << "> Polling (end of dequeue iteration)" << std::endl;
        poller_.poll( 0 );

        return pkt;

    } while ( dequeue_from != QueueType::NONE );

    // return pkt;
}

/* This function applies any update to scheduler state needed before dequeue */
void DualQCoupledAQM::scheduler_update( void ) 
{
    // Apply the WRR credit change 
    if (scheduler_type_ == SchedulerType::WRR)
        dynamic_cast<WRRScheduler*>(scheduler_.get())->apply_credit_change();
}

bool DualQCoupledAQM::empty( void ) const
{
    return l4s_queue_.empty() && classic_queue_.empty();
}

std::string DualQCoupledAQM::to_string( void ) const
{
    return "dualPI2";
}

unsigned int DualQCoupledAQM::size_bytes( void ) const
{
    return l4s_queue_.size_bytes() + classic_queue_.size_bytes();
}

unsigned int DualQCoupledAQM::size_packets( void ) const
{
    return l4s_queue_.size_packets() + classic_queue_.size_packets();;
}

bool DualQCoupledAQM::can_mark_or_drop( void )
{
    if ( size_bytes() < 2 * MTU )
        return false;
    
    return true;
}

void DualQCoupledAQM::drop( DropReason reason )
{
    if (last_dequeue_from_ == QueueType::L4S) {
        drop_l4s_pkts_++;
    } else if (last_dequeue_from_ == QueueType::Classic) {
        drop_classic_pkts_++;
    }

    switch ( reason ) {
        case DropReason::Overflow:
            overflow_drop_pkts_++;
            break;
        case DropReason::Overload:
            overload_drop_pkts_++;
            break;
        case DropReason::NotECT:
            not_ect_drop_pkts_++;
            break;
    }
}

unsigned char DualQCoupledAQM::get_ecn_bits( QueuedPacket & p )
{
    struct iphdr *ip_header = (struct iphdr *) &p.contents[4];
    return ( ip_header->tos & IPTOS_ECN_MASK ) ; 
}

void DualQCoupledAQM::mark( QueuedPacket & p )
{
    //std::cout << ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> Mark() called!! " << std::endl;
    struct iphdr *ip_header = (struct iphdr *) &p.contents[4];

    //std::cout << "-- Previous tos: " << std::to_string(ip_header->tos) << std::endl;
    //std::cout << "-- Previous Checksum: " << ntohs(ip_header->check) << std::endl;

    ip_header->tos = ( ip_header->tos & ~IPTOS_ECN_MASK ) | ( IPTOS_ECN_CE & IPTOS_ECN_MASK );

    //std::cout << "-- New tos: " << std::to_string(ip_header->tos) << std::endl;

    // Zero out the checksum field before recalculating
    ip_header->check = 0;
    ip_header->check = calculate_ip_checksum ((unsigned short*) ip_header, ip_header->ihl << 2);

    struct iphdr *ip_header2 = (struct iphdr *) &p.contents[4];
    mark_pkts_++;

    //std::cout << "-- New Checksum: " << ntohs(ip_header->check) << " should be = "<< ntohs(ip_header2->check) << std::endl;
}

 /* Returns TRUE with a certain likelihood modeling a recurring (and deterministic) 
    pattern of marks/drops */ 
// bool DualQCoupledAQM::recur( AbstractDualPI2PacketQueue & queue, double likelihood )
// {
//     std::cout << "##### In recur !! likelihood is " << likelihood << std::endl;

//     double count = queue.get_recur_count() + likelihood;

//     std::cout << "##### The new count = " << count << std::endl;
//     if ( count > 1.0 ) {
//         //std::cout << "##### Count is higer than MAX_PROB. New count is: " << count - MAX_PROB << std::endl;
//         queue.set_recur_count( count - 1.0 );
//         return true;
//     }
//     queue.set_recur_count( count );
//     return false;
// }

bool DualQCoupledAQM::roll( double prob ) { 
    
    // Thread-local engine to mimic per-CPU PRNG state in the Linux kernel
    // Only runs once per thread
    static thread_local std::mt19937 engine(std::random_device{}());

    uint32_t rand_int = engine(); // uniformly distributed in [0, 2^32 - 1]
    double rand_double = static_cast<double>(rand_int) / static_cast<double>(UINT32_MAX);
    return ( rand_double <= prob );
 }

void DualQCoupledAQM::set_periodic_update( void ) 
{
    const timespec interval { 0, t_update_ms_ * NS_PER_MS };
    timer_.set_time( interval, interval );
   
    poller_.add_action( Poller::Action( timer_, Direction::In, 
                                        [&] () {                                         
                                            // cout << "set_periodic_update function called! " << endl;

                                            string str = timer_.read();
                                            // //std::cout << " ------ Timer read output " << str << std::endl;

                                            uint64_t now = timestamp();
                                            pp_ = calculate_base_aqm_prob ( now );
                                            p_c_ = pow( pp_, 2 );
                                            p_cl_ = pp_ * k_ ;

                                            // cout << "-- Updated Probs -- " << endl;
                                            // cout << "pp =  " << std::to_string(pp_) << endl;
                                            // cout << "p_c =  " << std::to_string(p_c_) << endl;
                                            // cout << "p_cl =  " << std::to_string(p_cl_) << endl;
                                            
                                            // cout << "queue size in bytes: " << size_bytes() << endl; 
                                            // cout << "queue size in packets: " << size_packets() << endl;
                                            
                                            // cout << "-- Updated Probs -- " << endl;
                                            // cout << "pp =  " << std::to_string(pp_) << endl;
                                            // cout << "p_c =  " << std::to_string(p_c_) << endl;
                                            // cout << "p_cl =  " << std::to_string(p_cl_) << endl;

                                            return ResultType::Continue;
                                        } ) ); 
}

// void DualQCoupledAQM::print_state( void )
// {
//     cout << "function called! " << endl;

// }

double DualQCoupledAQM::calculate_base_aqm_prob( uint64_t ref ) 
{
    /* From  RFC 9332   : dualpi2_update function
             Linux code : calculate_probability function  */

    // cout << "-- In calculate_base_aqm_prob (that outputs pp) " << endl;
    
    // cout << ">> alpha = " << std::to_string(alpha_) << endl;
    // cout << ">> beta = " << std::to_string(beta_) << endl;

    uint64_t qdelay_old = max( l4s_qdelay_ms_, classic_qdelay_ms_ ) ;

    // cout << ">> [old] l4s_qdelay_ms = " << std::to_string(l4s_qdelay_ms_) << endl;
    // cout << ">> [old] classic_qdelay_ms = " << std::to_string(classic_qdelay_ms_) << endl;

    

    // Update the qdelays
    l4s_qdelay_ms_ = l4s_queue_.qdelay_in_ms( ref );
    classic_qdelay_ms_ = classic_queue_.qdelay_in_ms( ref );

    uint64_t qdelay = max( l4s_qdelay_ms_, classic_qdelay_ms_ ) ;

    // cout << ">> [new] l4s_qdelay_ms = " << std::to_string(l4s_qdelay_ms_) << endl;
    // cout << ">> [new] classic_qdelay_ms = " << std::to_string(classic_qdelay_ms_) << endl;

    double new_pp = (static_cast<double>(qdelay) - target_ms_) * alpha_ +
                    (static_cast<double>(qdelay) - qdelay_old) * beta_  + pp_;

    // cout << ">> new_prob = " << std::to_string(new_prob) << endl;

    if ( new_pp > 1.0 ) {
        // prevent overflow
        new_pp = 1.0;
    }
    else if ( new_pp < 0.0) {
        // prevent underflow
        new_pp = 0.0;
    }

    // TODO: check the capping of p' if no drop on overload

    return new_pp;
}

// unsigned int DualQCoupledAQM::get_arg( const string & args, const string & name )
// {
//     auto offset = args.find( name );
//     if ( offset == string::npos ) {
//         return 0; /* default value */
//     } else {
//         /* extract the value */

//         /* advance by length of name */
//         offset += name.size();

//         /* make sure next char is "=" */
//         if ( args.substr( offset, 1 ) != "=" ) {
//             throw runtime_error( "could not parse queue arguments: " + args + " name: " + name + "substr: " + args.substr( offset, 1 ));
//         }

//         /* advance by length of "=" */
//         offset++;

//         /* find the first non-digit character */
//         auto offset2 = args.substr( offset ).find_first_not_of( "0123456789" );

//         auto digit_string = args.substr( offset ).substr( 0, offset2 );

//         if ( digit_string.empty() ) {
//             throw runtime_error( "could not parse queue arguments: " + args );
//         }
      
//         return myatoi( digit_string );
//     }
// }

DualQCoupledAQM::~DualQCoupledAQM ( void )
{
    update_running_ = false;

    // if (periodic_worker_.joinable()) {
    //     periodic_worker_.join();
    // }
    

    // TODO: make sure there's resource freeing in case of interrupt
}
