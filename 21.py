import random


# Card deck setups

SUITS = ["Hearts", "Diamonds", "Clubs", "Spades"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]


def create_deck():
    return [{"rank": rank, "suit": suit} for suit in SUITS for rank in RANKS]


def get_card_value(card):
    rank = card["rank"]
    if rank == "A":
        return 11
    if rank in ["J", "Q", "K"]:
        return 10
    return int(rank)


def calculate_hand_value(hand):
    total = 0
    aces = 0

    for card in hand:
        total += get_card_value(card)
        if card["rank"] == "A":
            aces += 1

    while total > 21 and aces:
        total -= 10
        aces -= 1

    return total


def show_hand(hand, hidden=False):
    if hidden:
        print("Dealer's hand: [Hidden]", end=" | ")
        print_card(hand[1])
        print()
    else:
        print("Dealer's hand:", end=" ")
        for card in hand:
            print_card(card)
        print(f"(Total: {calculate_hand_value(hand)})")


def print_card(card):
    print(f"{card['rank']} of {card['suit']}", end=" | ")


def deal_card(deck):
    return deck.pop(random.randrange(len(deck)))


def get_bet(paperclips):
    while True:
        try:
            bet_input = input(f"You have {paperclips} paperclips. Bet amount (or 'max'): ").strip().lower()
            if bet_input == "max":
                return paperclips

            bet = int(bet_input)
            if 1 <= bet <= paperclips:
                return bet
            print(f"Enter a number from 1 to {paperclips}, or type 'max'.")
        except ValueError:
            print("Please enter a whole number or type 'max'.")


def blackjack_game(paperclips):
    bet = get_bet(paperclips)
    deck = create_deck()
    random.shuffle(deck)

    player_hand = [deal_card(deck), deal_card(deck)]
    dealer_hand = [deal_card(deck), deal_card(deck)]

    print("Welcome to Blackjack!")
    print("Your goal: beat the dealer without going over 21.")

    def play_hand(hand, can_split=True):
        """Play one hand and return its result."""
        while True:
            print("\nYour hand:")
            for card in hand:
                print_card(card)
            player_total = calculate_hand_value(hand)
            print(f"\nTotal: {player_total}")
            show_hand(dealer_hand, hidden=True)

            if player_total == 21:
                return "blackjack" if len(hand) == 2 and can_split else "stand"
            if player_total > 21:
                print("Bust! Dealer wins.")
                return "bust"

            options = "h/s"
            if (can_split and len(hand) == 2 and
                    hand[0]["rank"] == hand[1]["rank"] and
                    paperclips >= bet * 2):
                options = "h/s/split"
            choice = input(f"Hit, stand{', or split' if 'split' in options else ''}? (h/s{ '/split' if 'split' in options else ''}): ").lower()

            if choice == "h":
                hand.append(deal_card(deck))
            elif choice == "s":
                return "stand"
            elif choice == "split" and "split" in options:
                return "split"
            else:
                print("Invalid input.")

    results = []
    if (player_hand[0]["rank"] == player_hand[1]["rank"]
            and paperclips >= bet * 2):
        first_result = play_hand(player_hand)
        if first_result == "split":
            second_hand = [player_hand.pop(), deal_card(deck)]
            player_hand.append(deal_card(deck))
            print("\nHand split!")
            results.append(play_hand(player_hand, can_split=False))
            results.append(play_hand(second_hand, can_split=False))
        else:
            results.append(first_result)
    else:
        results.append(play_hand(player_hand, can_split=False))

    # Dealer's turn
    print("\nDealer reveals cards:")
    show_hand(dealer_hand)

    while calculate_hand_value(dealer_hand) < 17:
        dealer_hand.append(deal_card(deck))
        print("Dealer hits:")
        show_hand(dealer_hand)

    dealer_total = calculate_hand_value(dealer_hand)
    balance_change = 0
    for result in results:
        if result == "blackjack":
            print("Blackjack! You win!")
            balance_change += bet * 3 // 2
        elif result == "bust":
            balance_change -= bet
        else:
            player_total = calculate_hand_value(player_hand)
            if dealer_total > 21 or player_total > dealer_total:
                print("You win!")
                balance_change += bet
            elif player_total < dealer_total:
                print("Dealer wins!")
                balance_change -= bet
            else:
                print("It's a tie!")
    return paperclips + balance_change


paperclips = 100
debt = 0
while True:
    paperclips = blackjack_game(paperclips)
    print(f"Paperclips: {paperclips}")

    if debt > 0 and paperclips > 0:
        print(f"Loan balance: {debt} paperclips.")
        while True:
            payment_input = input(
                "Payment amount, 'all' to pay off the debt, or 0 to skip: "
            ).strip().lower()
            try:
                if payment_input == "all":
                    if debt > paperclips:
                        print("You do not have enough paperclips to pay off all the debt.")
                        continue
                    payment = debt
                else:
                    payment = int(payment_input)
                if 0 <= payment <= min(paperclips, debt):
                    paperclips -= payment
                    debt -= payment
                    print(f"Loan balance: {debt} paperclips. Paperclips: {paperclips}")
                    break
                print(f"Enter an amount from 0 to {min(paperclips, debt)}, 'all', or 0.")
            except ValueError:
                print("Please enter a whole number, 'all', or 0.")

    if debt > 0:
        print(f"You owe {debt} paperclips.")
    again = input("Play again? (y/n): ").lower()
    if again != "y":
        print("Thanks for playing Blackjack!")
        break

    if paperclips <= 0:
        while True:
            loan_input = input("You have no paperclips. Take a loan? Enter an amount (or 'n'): ").strip().lower()
            if loan_input == "n":
                print("You ran out of coins. Game over!")
                raise SystemExit
            try:
                loan = int(loan_input)
                if loan > 0:
                    paperclips += loan
                    debt += loan
                    print(f"Loan received. Paperclips: {paperclips}")
                    print(f"You now owe {debt} paperclips.")
                    break
                print("Enter a positive loan amount, or type 'n'.")
            except ValueError:
                print("Please enter a whole number or type 'n'.")


