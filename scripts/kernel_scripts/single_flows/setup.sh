sudo ip netns add ns_s
sudo ip netns add ns_r
sudo ip link add veth-s type veth peer name veth-r
sudo ip link set veth-s netns ns_s
sudo ip link set veth-r netns ns_r
sudo ip netns exec ns_s ip addr add 172.20.1.1/30 dev veth-s # sender
sudo ip netns exec ns_r ip addr add 172.20.1.2/30 dev veth-r # receiver
sudo ip netns exec ns_s ip link set lo up
sudo ip netns exec ns_r ip link set lo up
sudo ip netns exec ns_s ip link set veth-s up
sudo ip netns exec ns_r ip link set veth-r up

#disable offloads and sender egress interface
sudo ip netns exec ns_s ethtool -K veth-s tso off gso off gro off lro off

# 1) Delete existing qdisc
sudo ip netns exec ns_s ../tc/tc qdisc del dev veth-s root 2>/dev/null

# 2) Add root HTB qdisc
sudo ip netns exec ns_s ../tc/tc qdisc add dev veth-s root handle 1: htb default 10

# 3) Add HTB class with rate limit
sudo ip netns exec ns_s ../tc/tc class add dev veth-s parent 1: classid 1:10 htb rate 12mbit ceil 12mbit burst 15k

# 4) Attach DualPI2 to the HTB class
sudo ip netns exec ns_s ../tc/tc qdisc add dev veth-s parent 1:10 handle 2: dualpi2 target 5ms step_thresh 1ms limit 200

# Verify the configuration
sudo ip netns exec ns_s ../tc/tc  -s qdisc show dev veth-s

# set TCP ECN and congestion control to Prague in sender namespace
ip netns exec ns_s sysctl -w net.ipv4.tcp_ecn=1
ip netns exec ns_s sysctl -w net.ipv4.tcp_congestion_control=prague

# Check
ip netns exec ns_s sysctl net.ipv4.tcp_available_congestion_control
